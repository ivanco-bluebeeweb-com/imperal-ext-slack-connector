"""Read tools for the workspace directory: workspaces, channels, people, access.

These answer "what is there and what can I reach" -- the questions asked
before any message is read. `check_access` lives here because it exists purely
to explain why something visible in Slack is not visible to the app.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acc
import journal
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
_resolve_user = shared.resolve_user_or_error


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

    # READABLE is the honest measure, and it is NOT the same as `is_member`.
    # Slack reports `is_member: false` for direct messages, so counting
    # membership alone described a workspace where the app could read DMs
    # perfectly well as "member of 1" -- which is what led to the false
    # conclusion that DMs were unreachable. Verified live: the DM with the
    # account owner has is_member false, and reading and posting both work.
    readable = [c for c in visible
                if isinstance(c, dict) and journal.is_reachable(c)[0]]
    dms_readable = [c for c in readable
                    if c.get("is_im") or c.get("is_mpim")]

    kind = sc.token_kind(token)
    scopes = str(info.get("scopes") or "")
    can_search = kind == "user"

    gaps: list[str] = []
    if not can_search:
        gaps.append("message search (needs a user token)")
    if not readable:
        gaps.append("reading history or posting (the app is in no channel and "
                    "has no direct messages yet)")
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
        conversations_readable=len(readable),
        dms_readable=len(dms_readable),
        can_search=can_search,
        granted_scopes=scopes,
        missing_for_common_tasks="; ".join(gaps),
        explanation=explanation,
    )
    return ActionResult.success(
        summary=f"Connected to {report.workspace_name or 'Slack'} as "
        f"{report.identity or 'the app'}: {len(visible)} conversation(s) "
        f"visible, {len(readable)} readable "
        f"({len(joined)} channel(s) joined, {len(dms_readable)} direct "
        f"message(s)).",
        data=report)
