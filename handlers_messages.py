"""Read tools for message content: channel history, threads, search.

This is where the connector earns its keep: mentions and links come back
resolved into names, so history is readable by a person and summarisable by a
model. Raw `<@U024BE7LH>` never reaches the caller.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acc
import shared
import slack_client as sc
import slack_objects as so
from app import chat
from models import (
    MessageList,
    MessageRecord,
    ReadChannelParams,
    ReadThreadParams,
    SearchHit,
    SearchMessagesParams,
    SearchResults,
)

MEMBERSHIP_NOTE = shared.MEMBERSHIP_NOTE
_error = shared.error
_from_envelope = shared.from_envelope
_resolve = shared.resolve
_resolve_channel = shared.resolve_channel_or_error
_resolve_user = shared.resolve_user_or_error


@chat.function(
    "read_channel",
    "Read recent messages in a Slack channel by name -- actual text, with "
    "mentions and links resolved.",
    action_type="read", chain_callable=True,
    data_model=MessageRecord,
)
async def read_channel(ctx, params: ReadChannelParams) -> ActionResult:
    """Read a channel's recent history, newest first."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    target, err = await _resolve_channel(ctx, token, params.channel)
    if err:
        return err

    out = await sc.request(
        ctx, "GET", "conversations.history", token,
        params={"channel": target["id"], "limit": min(params.limit, sc.MAX_PAGE_SIZE)})
    if not out.get("ok"):
        return _from_envelope(out)

    raw = out.get("data", {}).get("messages") or []
    users, channels = await acc.name_maps(ctx, token)

    rows: list[MessageRecord] = []
    for msg in raw:
        if not isinstance(msg, dict):
            continue
        thread_ts, reply_count = so.thread_info(msg)
        rows.append(MessageRecord(
            text=so.message_text(msg, users, channels),
            author=so.author_of(msg, users),
            author_id=str(msg.get("user") or msg.get("bot_id") or ""),
            ts=str(msg.get("ts") or ""),
            posted_at=so.humanize_ts(str(msg.get("ts") or "")),
            thread_ts=thread_ts,
            reply_count=reply_count if params.include_thread_counts else 0,
            reactions=so.reactions_of(msg),
            is_thread_parent=bool(thread_ts and thread_ts == msg.get("ts")),
        ))

    if not rows:
        return ActionResult.success(
            summary=f"#{target['name']} has no messages the app can read. "
            f"{MEMBERSHIP_NOTE}",
            data=MessageList(channel=target["name"], channel_id=target["id"],
                             count=0, note=MEMBERSHIP_NOTE))

    threads = sum(1 for r in rows if r.reply_count)
    summary = f"{len(rows)} message(s) from #{target['name']}"
    if threads:
        summary += f", {threads} with replies"
    return ActionResult.success(summary=summary + ".", data=MessageList(
        channel=target["name"], channel_id=target["id"],
        messages=rows, count=len(rows),
        has_more=bool(out.get("data", {}).get("has_more"))))


@chat.function(
    "read_thread",
    "Read a Slack thread: the parent message and all its replies in order.",
    action_type="read", chain_callable=True,
    data_model=MessageRecord,
)
async def read_thread(ctx, params: ReadThreadParams) -> ActionResult:
    """Read one thread in full."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    target, err = await _resolve_channel(ctx, token, params.channel)
    if err:
        return err

    ts = (params.ts or "").strip()
    if not ts:
        return _error(
            "A thread needs the timestamp of its parent message. Read the "
            "channel first -- each message carries its ts.",
            sc.SLACK_VALIDATION_FAILED)

    out = await sc.request(
        ctx, "GET", "conversations.replies", token,
        params={"channel": target["id"], "ts": ts,
                "limit": min(params.limit, sc.MAX_PAGE_SIZE)})
    if not out.get("ok"):
        return _from_envelope(out)

    raw = out.get("data", {}).get("messages") or []
    if not raw:
        return _error(
            f"No thread found at that timestamp in #{target['name']}. The ts "
            "must be the PARENT message's, copied exactly.",
            sc.SLACK_MESSAGE_NOT_FOUND)

    users, channels = await acc.name_maps(ctx, token)
    rows = [
        MessageRecord(
            text=so.message_text(msg, users, channels),
            author=so.author_of(msg, users),
            author_id=str(msg.get("user") or msg.get("bot_id") or ""),
            ts=str(msg.get("ts") or ""),
            posted_at=so.humanize_ts(str(msg.get("ts") or "")),
            thread_ts=str(msg.get("thread_ts") or ""),
            reactions=so.reactions_of(msg),
            is_thread_parent=(str(msg.get("ts") or "") == ts),
        )
        for msg in raw if isinstance(msg, dict)
    ]

    replies = max(len(rows) - 1, 0)
    return ActionResult.success(
        summary=f"Thread in #{target['name']}: {replies} repl{'y' if replies == 1 else 'ies'}.",
        data=MessageList(channel=target["name"], channel_id=target["id"],
                         messages=rows, count=len(rows),
                         has_more=bool(out.get("data", {}).get("has_more"))))


@chat.function(
    "search_messages",
    "Search Slack messages across the workspace. Needs a user token -- Slack "
    "does not offer search to bot tokens.",
    action_type="read", chain_callable=True,
    data_model=SearchHit,
)
async def search_messages(ctx, params: SearchMessagesParams) -> ActionResult:
    """Search messages, explaining up front when the token cannot do it."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    # Fail EARLY and legibly. Slack answers `not_allowed_token_type` for a bot
    # token here, which reads like a bug rather than a documented limit -- so
    # say what is actually required before spending the call.
    if sc.token_kind(token) != "user":
        return _error(
            "Slack message search is only available to a user token (xoxp-); "
            "it does not exist for bot tokens. Connect a user token for this "
            "workspace to search, or read a specific channel instead.",
            sc.SLACK_WRONG_TOKEN_TYPE)

    query = (params.query or "").strip()
    if not query:
        return _error("Give me something to search for.",
                      sc.SLACK_VALIDATION_FAILED)

    out = await sc.request(
        ctx, "GET", "search.messages", token,
        params={"query": query, "count": min(params.limit, 100)})
    if not out.get("ok"):
        return _from_envelope(out)

    block = out.get("data", {}).get("messages") or {}
    matches = block.get("matches") or []
    users, channels = await acc.name_maps(ctx, token)

    hits = [
        SearchHit(
            text=so.message_text(m, users, channels),
            author=so.author_of(m, users),
            channel=str((m.get("channel") or {}).get("name") or ""),
            ts=str(m.get("ts") or ""),
            posted_at=so.humanize_ts(str(m.get("ts") or "")),
            permalink=so.permalink_of(m),
        )
        for m in matches if isinstance(m, dict)
    ]

    if not hits:
        return ActionResult.success(
            summary=f"Nothing matches '{query}'.",
            data=SearchResults(query=query, count=0))

    total = int(block.get("total") or len(hits))
    return ActionResult.success(
        summary=f"{len(hits)} match(es) for '{query}'"
        + (f" of {total} total." if total > len(hits) else "."),
        data=SearchResults(query=query, hits=hits, count=len(hits),
                           total_available=total))
