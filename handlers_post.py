"""Write tools for messages: connect, send, edit, delete, react, pin.

Every write resolves the target channel by NAME first and refuses to guess
between several matches -- there is no undo for a message in the wrong channel.
`connect_workspace` lives here because connecting is the write that unblocks
everything else.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acc
import shared
import slack_client as sc
import slack_objects as so
from app import chat
from models import (
    ConnectWorkspaceParams,
    DeleteMessageParams,
    EditMessageParams,
    MessageAck,
    PinParams,
    ReactionParams,
    SendMessageParams,
    ChannelAck,
)

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
