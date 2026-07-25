"""Read tools: workspaces, channels, message history, threads, search, users,
access report.

Readable first: reading has to be genuinely useful before any write flow
matters. So `read_channel` returns ACTUAL MESSAGE TEXT with mentions and links
resolved into names -- not raw `<@U024BE7LH>` -- and `check_access` exists
purely to explain why something the user can plainly see in Slack is not
visible here.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acc
import shared
import slack_client as sc
import slack_objects as so
from app import chat
from models import (
    AccessReport,
    ChannelList,
    ChannelRecord,
    CheckAccessParams,
    ListChannelsParams,
    ListUsersParams,
    ListWorkspacesParams,
    MessageList,
    MessageRecord,
    ReadChannelParams,
    ReadThreadParams,
    SearchHit,
    SearchMessagesParams,
    SearchResults,
    UserList,
    UserRecord,
    WorkspaceList,
    WorkspaceRecord,
)

MEMBERSHIP_NOTE = shared.MEMBERSHIP_NOTE
_error = shared.error
_from_envelope = shared.from_envelope
_resolve = shared.resolve
_resolve_channel = shared.resolve_channel_or_error


@chat.function(
    "list_workspaces",
    "List the connected Slack workspaces and whether each token still works.",
    action_type="read", chain_callable=True,
    data_model=WorkspaceRecord,
)
async def list_workspaces(ctx, params: ListWorkspacesParams) -> ActionResult:
    """List connected Slack workspaces and verify each token still works."""
    records = await acc.list_workspaces(ctx, refresh=params.refresh)
    if not records:
        return _error(
            "No Slack token is configured yet. Create an app at "
            "api.slack.com/apps, install it to your workspace, then paste the "
            "token on the Connect Slack screen.",
            sc.SLACK_TOKEN_MISSING)

    entity = WorkspaceList(
        workspaces=[WorkspaceRecord(**r) for r in records],
        count=len(records),
        note=("Every workspace is a separate Slack app install, so each one has "
              "its own token." if len(records) > 1 else ""),
    )
    healthy = [r for r in records if r.get("status") == "ok"]
    if not healthy:
        return _error(
            "Slack rejected every configured token. Each one may have been "
            "revoked, or the app was uninstalled from the workspace.",
            sc.SLACK_TOKEN_REJECTED)

    return ActionResult.success(
        summary=f"{len(healthy)} Slack workspace(s) connected.", data=entity)


@chat.function(
    "list_channels",
    "List Slack channels and conversations the app can see, with topics and "
    "whether the app is a member.",
    action_type="read", chain_callable=True,
    data_model=ChannelRecord,
)
async def list_channels(ctx, params: ListChannelsParams) -> ActionResult:
    """List conversations, optionally filtered by name or kind."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    kinds = {
        "public": "public_channel",
        "private": "private_channel",
        "dm": "im",
        "group_dm": "mpim",
    }
    wanted = (params.kind or "").strip().lower()
    if wanted and wanted not in kinds:
        return _error(
            f"'{params.kind}' is not a kind of Slack conversation. Use "
            "'public', 'private', 'dm' or 'group_dm', or leave it empty.",
            sc.SLACK_VALIDATION_FAILED)
    types = kinds[wanted] if wanted else ",".join(kinds.values())

    out = await sc.paginate(
        ctx, "GET", "conversations.list", token,
        params={"types": types, "exclude_archived": not params.include_archived},
        results_key="channels", limit=max(params.limit * 4, 200))
    if not out.get("ok"):
        return _from_envelope(out)

    channels = out.get("results") or []
    needle = (params.query or "").strip().lower().lstrip("#")
    rows: list[ChannelRecord] = []
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        name = so.channel_name(ch)
        if needle and needle not in name.lower():
            continue
        is_member = bool(ch.get("is_member"))
        if params.member_only and not is_member:
            continue
        rows.append(ChannelRecord(
            name=name,
            channel_id=str(ch.get("id") or ""),
            kind=so.channel_kind(ch),
            topic=str((ch.get("topic") or {}).get("value") or ""),
            purpose=str((ch.get("purpose") or {}).get("value") or ""),
            member_count=int(ch.get("num_members") or 0),
            is_member=is_member,
            is_archived=bool(ch.get("is_archived")),
        ))
        if len(rows) >= params.limit:
            break

    if not rows:
        detail = (f"No conversation matches '{params.query}'."
                  if params.query else "No conversations are visible.")
        return ActionResult.success(summary=f"{detail} {MEMBERSHIP_NOTE}",
                               data=ChannelList(count=0, note=MEMBERSHIP_NOTE))

    joined = sum(1 for r in rows if r.is_member)
    return ActionResult.success(
        summary=f"{len(rows)} conversation(s); the app is in {joined} of them.",
        data=ChannelList(
            channels=rows, count=len(rows),
            has_more=bool(out.get("has_more")),
            note="" if joined else MEMBERSHIP_NOTE))


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


@chat.function(
    "list_users",
    "List people in the Slack workspace, with names, emails and titles.",
    action_type="read", chain_callable=True,
    data_model=UserRecord,
)
async def list_users(ctx, params: ListUsersParams) -> ActionResult:
    """List workspace members, optionally filtered."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    out = await sc.paginate(
        ctx, "GET", "users.list", token, results_key="members",
        limit=max(params.limit * 4, 200))
    if not out.get("ok"):
        return _from_envelope(out)

    needle = (params.query or "").strip().lower().lstrip("@")
    rows: list[UserRecord] = []
    for member in out.get("results") or []:
        if not isinstance(member, dict):
            continue
        if member.get("deleted") and not needle:
            continue
        is_bot = bool(member.get("is_bot")) or member.get("id") == "USLACKBOT"
        if is_bot and not params.include_bots:
            continue
        profile = member.get("profile") or {}
        display = so.user_display_name(member)
        email = str(profile.get("email") or "")
        if needle and needle not in display.lower() and needle not in email.lower():
            continue
        rows.append(UserRecord(
            display_name=display,
            real_name=str(profile.get("real_name") or member.get("real_name") or ""),
            user_id=str(member.get("id") or ""),
            email=email,
            job_title=str(profile.get("title") or ""),
            timezone=str(member.get("tz") or ""),
            is_bot=is_bot,
            is_admin=bool(member.get("is_admin")),
            is_deactivated=bool(member.get("deleted")),
        ))
        if len(rows) >= params.limit:
            break

    if not rows:
        detail = (f"Nobody matches '{params.query}'."
                  if params.query else "No members are visible.")
        return ActionResult.success(summary=detail, data=UserList(count=0))

    return ActionResult.success(summary=f"{len(rows)} member(s).", data=UserList(
        users=rows, count=len(rows), has_more=bool(out.get("has_more"))))


@chat.function(
    "check_access",
    "Report what this Slack app can currently reach, and explain why anything "
    "missing is not visible.",
    action_type="read", chain_callable=True,
    data_model=AccessReport,
)
async def check_access(ctx, params: CheckAccessParams) -> ActionResult:
    """Explain reachability -- the antidote to a mysteriously empty result."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    info = await acc.identify(ctx, token)
    if not info.get("ok"):
        return _from_envelope(info)

    out = await sc.paginate(
        ctx, "GET", "conversations.list", token,
        params={"types": "public_channel,private_channel,im,mpim",
                "exclude_archived": True},
        results_key="channels", limit=1000)
    visible = out.get("results") or [] if out.get("ok") else []
    joined = [c for c in visible if isinstance(c, dict) and c.get("is_member")]

    kind = sc.token_kind(token)
    scopes = str(info.get("scopes") or "")
    can_search = kind == "user"

    gaps: list[str] = []
    if not can_search:
        gaps.append("message search (needs a user token)")
    if not joined:
        gaps.append("reading history or posting (the app is in no channel yet)")
    if scopes and "channels:history" not in scopes and "groups:history" not in scopes:
        gaps.append("reading history (needs a channels:history scope)")
    if scopes and "chat:write" not in scopes:
        gaps.append("posting messages (needs the chat:write scope)")

    explanation = MEMBERSHIP_NOTE
    if not can_search:
        explanation += (" Message search is a user-token feature; Slack does "
                        "not expose it to bot tokens at all.")

    report = AccessReport(
        workspace_name=str(info.get("team") or ""),
        identity=str(info.get("identity") or ""),
        token_kind=kind,
        channels_visible=len(visible),
        channels_joined=len(joined),
        can_search=can_search,
        granted_scopes=scopes,
        missing_for_common_tasks="; ".join(gaps),
        explanation=explanation,
    )
    return ActionResult.success(
        summary=f"Connected to {report.workspace_name or 'Slack'} as "
        f"{report.identity or 'the app'}: {len(visible)} conversation(s) "
        f"visible, member of {len(joined)}.",
        data=report)
