"""Write tools: connect a workspace, send and edit messages, react, pin,
create channels and invite people.

Every write emits an effect so automations can build on top of it, and every
write that targets a channel or person resolves the NAME first and refuses to
guess between several matches -- posting into the wrong channel is not
recoverable by an undo.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acc
import shared
import slack_client as sc
import slack_objects as so
from app import chat
from models import (
    ChannelAck,
    ConnectWorkspaceParams,
    CreateChannelParams,
    DeleteMessageParams,
    EditMessageParams,
    InviteParams,
    MessageAck,
    PinParams,
    ReactionParams,
    SendMessageParams,
    SetTopicParams,
)
from models import WorkspaceRecord  # noqa: F401  (data_model for connect)

MEMBERSHIP_NOTE = shared.MEMBERSHIP_NOTE
_error = shared.error
_from_envelope = shared.from_envelope
_resolve = shared.resolve
_resolve_channel = shared.resolve_channel_or_error
_resolve_user = shared.resolve_user_or_error


class _ConnectAck(ChannelAck):
    """Reuses the ack shape; `name` carries the workspace name."""


@chat.function(
    "connect_workspace",
    "Connect a Slack workspace by saving its token, after checking the token "
    "actually works.",
    action_type="write", chain_callable=True,
    effects=["slack.workspace.connected"],
    event="slack-connector.connect_workspace",
    data_model=_ConnectAck,
)
async def connect_workspace(ctx, params: ConnectWorkspaceParams) -> ActionResult:
    """Verify a token against Slack, then store it.

    VERIFY BEFORE STORE, deliberately. Storing first and validating later is
    how a connector ends up "connected" to a token Slack rejects: the user gets
    a success message and a broken app. `auth.test` is the cheapest possible
    proof that a token is live, and it also yields the workspace name -- so the
    same call that validates is the one that names the workspace.
    """
    token = (params.token or "").strip()
    if not token:
        return _error(
            "Paste the Slack token first. Create an app at api.slack.com/apps, "
            "install it to the workspace, and copy the token from OAuth & "
            "Permissions.",
            sc.SLACK_VALIDATION_FAILED)

    kind = sc.token_kind(token)
    if kind == "unknown":
        return _error(
            "That does not look like a Slack app token. A bot token starts "
            "with 'xoxb-' and a user token with 'xoxp-'; both are on the app's "
            "OAuth & Permissions page.",
            sc.SLACK_VALIDATION_FAILED)

    info = await acc.identify(ctx, token)
    if not info.get("ok"):
        # Nothing is stored on a rejected token: a token that cannot answer
        # auth.test cannot do anything else either.
        return _from_envelope(info)

    stored = await acc.append_token(ctx, token)
    if not stored.get("ok"):
        return _from_envelope(stored)

    team = str(info.get("team") or "your workspace")
    identity = str(info.get("identity") or "the app")
    already = bool(stored.get("already_present"))
    total = int(stored.get("count") or 1)

    detail = (
        f"Already connected as {identity}; nothing changed."
        if already else
        f"Connected as {identity}"
        + (f" ({kind} token)." if kind else ".")
    )
    if not already and kind == "bot":
        detail += (" Invite the app to the channels it should reach: open a "
                   "channel in Slack and type /invite @your-app.")
    if not already and kind == "user":
        detail += " A user token can also search messages."

    return ActionResult.success(
        summary=f"{team}: {detail}",
        data=_ConnectAck(name=team, channel_id=str(info.get("team_id") or ""),
                         action="connected" if not already else "unchanged",
                         detail=f"{total} workspace(s) configured."),
        refresh_panels=["slack", "slack_nav"],
    )


@chat.function(
    "send_message",
    "Send a Slack message to a channel or person, or reply inside a thread.",
    action_type="write", chain_callable=True,
    effects=["slack.message.sent"],
    event="slack-connector.send_message",
    data_model=MessageAck,
)
async def send_message(ctx, params: SendMessageParams) -> ActionResult:
    """Post a message; a thread_ts turns it into a threaded reply."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    text = params.text or ""
    if not text.strip():
        return _error("A message needs some text.", sc.SLACK_VALIDATION_FAILED)

    target, err = await _resolve_channel(ctx, token, params.channel)
    if err:
        return err

    body: dict = {"channel": target["id"], "text": text,
                  "unfurl_links": params.unfurl_links}
    thread_ts = (params.thread_ts or "").strip()
    if thread_ts:
        body["thread_ts"] = thread_ts
        if params.reply_broadcast:
            body["reply_broadcast"] = True

    out = await sc.request(ctx, "POST", "chat.postMessage", token, json=body)
    if not out.get("ok"):
        return _from_envelope(out)

    data = out.get("data") or {}
    ts = str(data.get("ts") or "")
    where = shared.channel_label(target)
    return ActionResult.success(
        summary=(f"Replied in the thread in {where}." if thread_ts
         else f"Sent to {where}."),
        data=MessageAck(channel=target["name"], channel_id=target["id"], ts=ts,
                        action="replied" if thread_ts else "sent",
                        detail=so.humanize_ts(ts)))


@chat.function(
    "edit_message",
    "Edit the text of a Slack message the app posted.",
    action_type="write", chain_callable=True,
    effects=["slack.message.updated"],
    event="slack-connector.edit_message",
    data_model=MessageAck,
)
async def edit_message(ctx, params: EditMessageParams) -> ActionResult:
    """Replace a message's text."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    ts = (params.ts or "").strip()
    if not ts:
        return _error(
            "Editing needs the timestamp of the message. Read the channel "
            "first -- each message carries its ts.",
            sc.SLACK_VALIDATION_FAILED)
    if not (params.text or "").strip():
        return _error("An edit needs the replacement text.",
                      sc.SLACK_VALIDATION_FAILED)

    target, err = await _resolve_channel(ctx, token, params.channel)
    if err:
        return err

    out = await sc.request(ctx, "POST", "chat.update", token,
                           json={"channel": target["id"], "ts": ts,
                                 "text": params.text})
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        summary=f"Edited the message in {shared.channel_label(target)}.",
        data=MessageAck(channel=target["name"], channel_id=target["id"],
                        ts=ts, action="edited"))


@chat.function(
    "delete_message",
    "Delete a Slack message the app posted.",
    action_type="destructive", chain_callable=True,
    effects=["slack.message.deleted"],
    event="slack-connector.delete_message",
    data_model=MessageAck,
)
async def delete_message(ctx, params: DeleteMessageParams) -> ActionResult:
    """Delete a message.

    action_type="destructive", not "write", because Slack deletion is FINAL --
    unlike Notion's trash there is no restore path. That classification is what
    makes the kernel's two-step confirmation guard intercept the call, so the
    gate is declared rather than hand-rolled.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    ts = (params.ts or "").strip()
    if not ts:
        return _error(
            "Deleting needs the timestamp of the message.",
            sc.SLACK_VALIDATION_FAILED)

    target, err = await _resolve_channel(ctx, token, params.channel)
    if err:
        return err

    out = await sc.request(ctx, "POST", "chat.delete", token,
                           json={"channel": target["id"], "ts": ts})
    if not out.get("ok"):
        return _from_envelope(out)

    return ActionResult.success(
        summary=f"Deleted the message in {shared.channel_label(target)}.",
        data=MessageAck(channel=target["name"], channel_id=target["id"],
                        ts=ts, action="deleted"))


@chat.function(
    "react_to_message",
    "Add or remove an emoji reaction on a Slack message.",
    action_type="write", chain_callable=True,
    effects=["slack.reaction.changed"],
    event="slack-connector.react_to_message",
    data_model=MessageAck,
)
async def react_to_message(ctx, params: ReactionParams) -> ActionResult:
    """Add or remove a reaction."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    ts = (params.ts or "").strip()
    emoji = so.normalize_emoji(params.emoji or "")
    if not ts:
        return _error("Reacting needs the timestamp of the message.",
                      sc.SLACK_VALIDATION_FAILED)
    if not emoji:
        return _error("Which emoji? Give its name, e.g. 'thumbsup'.",
                      sc.SLACK_VALIDATION_FAILED)

    target, err = await _resolve_channel(ctx, token, params.channel)
    if err:
        return err

    endpoint = "reactions.remove" if params.remove else "reactions.add"
    out = await sc.request(ctx, "POST", endpoint, token,
                           json={"channel": target["id"], "timestamp": ts,
                                 "name": emoji})
    if not out.get("ok"):
        return _from_envelope(out)

    verb = "Removed" if params.remove else "Added"
    return ActionResult.success(
        summary=f"{verb} :{emoji}: on the message in {shared.channel_label(target)}.",
        data=MessageAck(channel=target["name"], channel_id=target["id"], ts=ts,
                        action="unreacted" if params.remove else "reacted",
                        detail=emoji))


@chat.function(
    "pin_message",
    "Pin or unpin a Slack message in its channel.",
    action_type="write", chain_callable=True,
    effects=["slack.pin.changed"],
    event="slack-connector.pin_message",
    data_model=MessageAck,
)
async def pin_message(ctx, params: PinParams) -> ActionResult:
    """Pin or unpin a message."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    ts = (params.ts or "").strip()
    if not ts:
        return _error("Pinning needs the timestamp of the message.",
                      sc.SLACK_VALIDATION_FAILED)

    target, err = await _resolve_channel(ctx, token, params.channel)
    if err:
        return err

    endpoint = "pins.remove" if params.unpin else "pins.add"
    out = await sc.request(ctx, "POST", endpoint, token,
                           json={"channel": target["id"], "timestamp": ts})
    if not out.get("ok"):
        return _from_envelope(out)

    verb = "Unpinned" if params.unpin else "Pinned"
    return ActionResult.success(
        summary=f"{verb} the message in {shared.channel_label(target)}.",
        data=MessageAck(channel=target["name"], channel_id=target["id"], ts=ts,
                        action="unpinned" if params.unpin else "pinned"))


@chat.function(
    "create_channel",
    "Create a Slack channel, optionally private, with a topic and invitees.",
    action_type="write", chain_callable=True,
    effects=["slack.channel.created"],
    event="slack-connector.create_channel",
    data_model=ChannelAck,
)
async def create_channel(ctx, params: CreateChannelParams) -> ActionResult:
    """Create a channel, then optionally set a topic and invite people."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    name = so.normalize_channel_ref(params.name or "")
    if not name:
        return _error("A channel needs a name.", sc.SLACK_VALIDATION_FAILED)

    out = await sc.request(ctx, "POST", "conversations.create", token,
                           json={"name": name, "is_private": params.private})
    if not out.get("ok"):
        return _from_envelope(out)

    created = (out.get("data") or {}).get("channel") or {}
    channel_id = str(created.get("id") or "")
    actual = so.channel_name(created) or name
    notes: list[str] = []

    # Topic and invites are best-effort follow-ups: the channel already exists,
    # so a failure here must not read as "creation failed".
    if params.topic.strip() and channel_id:
        topic_out = await sc.request(
            ctx, "POST", "conversations.setTopic", token,
            json={"channel": channel_id, "topic": params.topic})
        if not topic_out.get("ok"):
            notes.append("the topic could not be set")

    invited: list[str] = []
    if params.invite.strip() and channel_id:
        ids: list[str] = []
        for ref in params.invite.split(","):
            ref = ref.strip()
            if not ref:
                continue
            person, perr = await _resolve_user(ctx, token, ref)
            if perr:
                notes.append(f"could not find '{ref}'")
                continue
            ids.append(person["id"])
            invited.append(person.get("name") or person["id"])
        if ids:
            inv_out = await sc.request(
                ctx, "POST", "conversations.invite", token,
                json={"channel": channel_id, "users": ",".join(ids)})
            if not inv_out.get("ok"):
                notes.append("the invites could not be sent")
                invited = []

    summary = f"Created {'private ' if params.private else ''}#{actual}"
    if invited:
        summary += f" and invited {', '.join(invited)}"
    summary += "."
    if notes:
        summary += " Note: " + "; ".join(notes) + "."

    return ActionResult.success(summary=summary, data=ChannelAck(
        name=actual, channel_id=channel_id, action="created",
        invited=", ".join(invited), detail="; ".join(notes)))


@chat.function(
    "invite_to_channel",
    "Invite people to a Slack channel by name.",
    action_type="write", chain_callable=True,
    effects=["slack.channel.updated"],
    event="slack-connector.invite_to_channel",
    data_model=ChannelAck,
)
async def invite_to_channel(ctx, params: InviteParams) -> ActionResult:
    """Invite one or more people to a channel."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    target, err = await _resolve_channel(ctx, token, params.channel)
    if err:
        return err

    ids: list[str] = []
    labels: list[str] = []
    unknown: list[str] = []
    for ref in (params.users or "").split(","):
        ref = ref.strip()
        if not ref:
            continue
        person, perr = await _resolve_user(ctx, token, ref)
        if perr:
            unknown.append(ref)
            continue
        ids.append(person["id"])
        labels.append(person.get("name") or person["id"])

    if not ids:
        return _error(
            "None of those people could be found in the workspace"
            + (f": {', '.join(unknown)}." if unknown else "."),
            sc.SLACK_USER_NOT_FOUND)

    out = await sc.request(ctx, "POST", "conversations.invite", token,
                           json={"channel": target["id"], "users": ",".join(ids)})
    if not out.get("ok"):
        return _from_envelope(out)

    summary = f"Invited {', '.join(labels)} to {shared.channel_label(target)}."
    if unknown:
        summary += f" Could not find: {', '.join(unknown)}."
    return ActionResult.success(summary=summary, data=ChannelAck(
        name=target["name"], channel_id=target["id"], action="invited",
        invited=", ".join(labels),
        detail=f"unknown: {', '.join(unknown)}" if unknown else ""))


@chat.function(
    "set_channel_topic",
    "Set a Slack channel's topic and/or purpose.",
    action_type="write", chain_callable=True,
    effects=["slack.channel.updated"],
    event="slack-connector.set_channel_topic",
    data_model=ChannelAck,
)
async def set_channel_topic(ctx, params: SetTopicParams) -> ActionResult:
    """Update a channel's topic and/or purpose."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    if not params.topic.strip() and not params.purpose.strip():
        return _error(
            "Nothing to change -- give a topic, a purpose, or both.",
            sc.SLACK_VALIDATION_FAILED)

    target, err = await _resolve_channel(ctx, token, params.channel)
    if err:
        return err

    changed: list[str] = []
    if params.topic.strip() or params.topic == "":
        out = await sc.request(ctx, "POST", "conversations.setTopic", token,
                               json={"channel": target["id"],
                                     "topic": params.topic})
        if not out.get("ok"):
            return _from_envelope(out)
        changed.append("topic")

    if params.purpose.strip():
        out = await sc.request(ctx, "POST", "conversations.setPurpose", token,
                               json={"channel": target["id"],
                                     "purpose": params.purpose})
        if not out.get("ok"):
            return _from_envelope(out)
        changed.append("purpose")

    return ActionResult.success(
        summary=f"Updated the {' and '.join(changed)} of {shared.channel_label(target)}.",
        data=ChannelAck(name=target["name"], channel_id=target["id"],
                        action="updated", detail=", ".join(changed)))
