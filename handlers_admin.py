"""Write tools for channel administration: create, invite, set topic.

Channel administration is separated from posting because the two carry
different risk: a wrong topic is embarrassing, a wrong invite exposes a private
channel to someone who should not see it.
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
    CreateChannelParams,
    InviteParams,
    SetTopicParams,
)

MEMBERSHIP_NOTE = shared.MEMBERSHIP_NOTE
_error = shared.error
_from_envelope = shared.from_envelope
_resolve = shared.resolve
_resolve_channel = shared.resolve_channel_or_error
_resolve_user = shared.resolve_user_or_error


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
