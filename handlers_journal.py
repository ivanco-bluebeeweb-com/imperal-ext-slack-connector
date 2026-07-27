"""Awareness tools: sweep Slack for what was said, and read what is remembered.

WHY A SWEEP EXISTS ALONGSIDE THE WEBHOOK.

The webhook is the right mechanism -- push is instant and costs nothing while
idle. But it is currently inert for two reasons, both verified against the live
platform: the signing secret is unset (every delivery refused), and the four
inbound event names are absent from the automations catalog, so even a
perfectly delivered event has no subscriber. The first is a paste; the second
is not ours to fix.

`catch_up` therefore reaches the same end state by POLLING, which depends on
none of that: it needs only the bot token that already works. Push and sweep
converge on ONE journal through ONE writer, so awareness is not "either/or" --
whichever path is available fills the same record, and when push starts working
the sweep simply finds nothing new.

WHAT THE SWEEP REFUSES TO DO. It never replies and never reacts. It walks
HISTORY, so a bug that answered things would answer a backlog in bulk, in
public, to people who wrote days ago. Reading is safe to automate; answering is
a decision, and it stays a separate, explicit one.
"""

from __future__ import annotations

from imperal_sdk import ActionResult

import accounts as acc
import inbound
import journal
import shared
import slack_client as sc
import slack_objects as so
from app import chat, ext
from models import (
    CatchUpParams,
    InboundLog,
    InboundMessage,
    ListInboundParams,
    SweepReport,
)

_error = shared.error
_from_envelope = shared.from_envelope
_resolve = shared.resolve


def _to_entity(row: dict) -> InboundMessage:
    """One journal row as the entity the caller sees."""
    return InboundMessage(
        text=str(row.get("text_readable") or row.get("text") or ""),
        author=str(row.get("user_display_name") or row.get("user_id") or ""),
        author_id=str(row.get("user_id") or ""),
        channel=str(row.get("channel_name") or row.get("channel_id") or ""),
        channel_id=str(row.get("channel_id") or ""),
        is_dm=bool(row.get("is_dm")),
        ts=str(row.get("message_ts") or ""),
        posted_at=str(row.get("posted_at") or ""),
        thread_ts=str(row.get("thread_ts") or ""),
        reply_thread_ts=str(row.get("reply_thread_ts") or ""),
        is_thread_reply=bool(row.get("is_thread_reply")),
        mention_of_bot=bool(row.get("mention_of_bot")),
        has_files=bool(row.get("has_files")),
        source=str(row.get("source") or ""),
        would_raise=str(row.get("event_names") or ""),
        permalink=str(row.get("permalink") or ""),
    )


@chat.function(
    "catch_up",
    "Sweep every Slack conversation the app can reach -- channels it was added "
    "to and direct messages -- and record anything said since the last sweep. "
    "Use this to make Webbee aware of messages without waiting for push events.",
    action_type="read", chain_callable=True,
    data_model=SweepReport,
)
async def catch_up(ctx, params: CatchUpParams) -> ActionResult:
    """Poll reachable conversations and journal what is new."""
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    self_user_id = str(workspace.get("identity_id") or "")
    self_bot_id = str(workspace.get("bot_id") or "")

    wanted_types: list[str] = []
    if params.include_channels:
        wanted_types += ["public_channel", "private_channel"]
    if params.include_dms:
        wanted_types += ["im", "mpim"]
    if not wanted_types:
        return _error(
            "Nothing to sweep: both channels and direct messages were "
            "excluded. Enable at least one.",
            sc.SLACK_VALIDATION_FAILED)

    listing = await sc.paginate(
        ctx, "GET", "conversations.list", token,
        params={"types": ",".join(wanted_types), "exclude_archived": True},
        results_key="channels",
        limit=max(journal.MAX_CONVERSATIONS, params.max_channels))
    if not listing.get("ok"):
        return _from_envelope(listing)

    conversations = [c for c in (listing.get("results") or [])
                     if isinstance(c, dict)]

    # Name maps once for the whole sweep, not per message: resolving <@U…> is
    # the same lookup table for every conversation, and fetching it per channel
    # would multiply the request count by the number of channels.
    users, channels = await acc.name_maps(ctx, token)

    reachable: list[dict] = []
    skipped: list[str] = []
    for conv in conversations:
        ok, why = journal.is_reachable(conv)
        if ok:
            reachable.append(conv)
        else:
            skipped.append(f"{so.channel_name(conv)} ({why})")

    # DMs first: a direct message is addressed to the app specifically, so if a
    # bounded sweep can only reach part of the workspace, that part should be
    # the conversations someone opened deliberately.
    reachable.sort(key=lambda c: 0 if journal.is_reachable(c)[0] and (
        so.channel_kind(c) in ("dm", "group_dm")) else 1)
    targets = reachable[:params.max_channels]

    examined = new = duplicate = ignored = 0
    swept: list[str] = []

    for conv in targets:
        channel_id = str(conv.get("id") or "")
        if not channel_id:
            continue

        history_params = {
            "channel": channel_id,
            "limit": min(params.limit_per_channel,
                         journal.MAX_MESSAGES_PER_CONVERSATION),
        }
        # `oldest` makes Slack do the filtering: without it a busy channel
        # returns the same old messages on every sweep and the dedupe check
        # pays a store read for each one.
        cursor = "" if params.full else await journal.cursor_for(ctx, channel_id)
        if cursor:
            history_params["oldest"] = cursor

        out = await sc.request(ctx, "GET", "conversations.history", token,
                               params=history_params)
        if not out.get("ok"):
            # One unreadable conversation must not abort the sweep: the other
            # conversations are still worth recording, and a partial sweep that
            # reports itself is more useful than a total failure.
            skipped.append(f"{so.channel_name(conv)} (could not be read)")
            continue

        messages = [m for m in (out.get("data", {}).get("messages") or [])
                    if isinstance(m, dict)]
        newest_ts = cursor
        found_here = 0

        for msg in messages:
            examined += 1
            message_ts = str(msg.get("ts") or "")

            # Slack's `oldest` is INCLUSIVE, so the cursor message itself comes
            # back on every sweep. Skipping it here keeps the dedupe path from
            # being exercised once per channel per sweep for no reason.
            if cursor and message_ts == cursor:
                continue

            noise, _reason = inbound.is_noise(
                msg, self_user_id=self_user_id, self_bot_id=self_bot_id)
            if noise:
                ignored += 1
                if message_ts and (not newest_ts or
                                   _greater(message_ts, newest_ts)):
                    newest_ts = message_ts
                continue

            normalised = journal.history_to_normalised(
                msg, conversation=conv, workspace=workspace,
                self_user_id=self_user_id, users=users, channels=channels)

            if await journal.record(ctx, normalised, source=journal.SOURCE_SWEEP):
                new += 1
                found_here += 1
            else:
                duplicate += 1

            if message_ts and (not newest_ts or _greater(message_ts, newest_ts)):
                newest_ts = message_ts

        if newest_ts:
            await journal.set_cursor(ctx, channel_id, newest_ts)
        if found_here:
            swept.append(f"{so.channel_name(conv)}: {found_here} new")

    try:
        await journal.prune(ctx)
    except Exception:
        pass

    totals = await journal.counts(ctx)

    if new:
        state = f"{new} new message(s) recorded"
    elif examined:
        state = "up to date"
    else:
        state = "nothing to read"

    lines = [
        f"Conversations reachable: {len(reachable)} of {len(conversations)} "
        f"visible; swept {len(targets)}.",
        f"Messages examined: {examined} — {new} newly recorded, "
        f"{duplicate} already known, {ignored} not human messages.",
        f"Journal now holds {totals['total']} message(s) across "
        f"{totals['channels']} conversation(s).",
    ]
    if swept:
        lines.append("New in: " + "; ".join(swept[:10]))
    if skipped:
        lines.append(
            f"Not swept ({len(skipped)}): " + "; ".join(skipped[:8])
            + ". A channel needs the app in it — open it in Slack and type "
              "/invite @imperal.")

    report = SweepReport(
        conversations_seen=len(conversations),
        conversations_swept=len(targets),
        conversations_skipped=len(skipped),
        messages_examined=examined,
        messages_new=new,
        messages_duplicate=duplicate,
        messages_ignored=ignored,
        skipped_detail="; ".join(skipped[:20]),
        swept_detail="; ".join(swept[:20]),
        detail="\n".join(lines),
        state=state,
    )
    return ActionResult.success(
        report,
        f"Slack catch-up: {state}. Examined {examined} message(s) in "
        f"{len(targets)} conversation(s); {new} newly recorded.")


def _greater(candidate: str, current: str) -> bool:
    """Whether Slack ts `candidate` is newer than `current`.

    Compared as floats, never as strings: '1785168241.7' sorts BEFORE
    '999999999.9' lexically, so a string comparison would park the cursor in
    the future and silently skip every later message.
    """
    try:
        return float(candidate) > float(current)
    except (TypeError, ValueError):
        return False


@chat.function(
    "list_inbound",
    "Show the Slack messages the app has recorded -- what was said, by whom, "
    "where, and whether it mentioned the app. Reads the journal; does not "
    "contact Slack.",
    action_type="read", chain_callable=True,
    data_model=InboundMessage,
)
async def list_inbound(ctx, params: ListInboundParams) -> ActionResult:
    """Read the journal, newest first."""
    channel_id = ""
    channel_label = ""
    if params.channel:
        # A name is resolved through Slack so the caller can say '#general';
        # if resolution fails the reference is used as-is, because a journalled
        # channel_id must stay readable even when the token is momentarily
        # unusable -- the journal is the one part that should keep working.
        token, _workspace, err = await _resolve(ctx, "")
        if not err:
            target, cerr = await shared.resolve_channel_or_error(
                ctx, token, params.channel)
            if not cerr:
                channel_id = str(target.get("id") or "")
                channel_label = str(target.get("name") or params.channel)
        if not channel_id:
            channel_id = so.normalize_channel_ref(params.channel)
            channel_label = params.channel

    rows = await journal.recent(
        ctx, limit=params.limit, channel_id=channel_id,
        dms_only=params.dms_only, mentions_only=params.mentions_only,
        unresolved_only=params.unanswered_only)
    totals = await journal.counts(ctx)

    messages = [_to_entity(r) for r in rows]

    filters = []
    if channel_label:
        filters.append(f"conversation {channel_label}")
    if params.dms_only:
        filters.append("direct messages only")
    if params.mentions_only:
        filters.append("mentions only")
    if params.unanswered_only:
        filters.append("unanswered only")
    filter_note = (" (" + ", ".join(filters) + ")") if filters else ""

    if totals["total"] == 0:
        note = (
            "Nothing recorded yet. Two things fill this: push delivery from "
            "Slack (needs the signing secret — see the Slack panel), or a "
            "manual sweep, which works right now: run catch_up.")
    elif not messages:
        note = ("Nothing matches that filter, though the journal holds "
                f"{totals['total']} message(s).")
    else:
        note = ""

    detail_lines = [
        f"Remembered: {totals['total']} message(s) across "
        f"{totals['channels']} conversation(s) — {totals['dms']} direct, "
        f"{totals['mentions']} mentioning the app.",
        f"Arrived by: {totals['from_push']} push, "
        f"{totals['from_sweep']} sweep.",
    ]

    log = InboundLog(
        messages=messages,
        count=len(messages),
        total_remembered=totals["total"],
        dms=totals["dms"],
        mentions=totals["mentions"],
        from_push=totals["from_push"],
        from_sweep=totals["from_sweep"],
        conversations=totals["channels"],
        note=note,
        detail="\n".join(detail_lines),
    )
    return ActionResult.success(
        log,
        f"{len(messages)} recorded Slack message(s){filter_note}; "
        f"{totals['total']} remembered in total."
        + (f" {note}" if note else ""))


# --- the sweep, running on its own ------------------------------------------
# Without this, awareness advances only when somebody CALLS catch_up -- which
# makes "Webbee knows what was said in Slack" true only in hindsight, at the
# moment of asking. An hourly schedule is what turns the journal from an archive
# you consult into awareness that keeps itself current, and it needs neither the
# signing secret nor an automations slot.
#
# Hourly rather than every few minutes on purpose: this polls somebody else's
# API on a recurring basis forever. One pass per hour over the reachable
# conversations is enough to keep a working record without turning a background
# task into sustained load on the workspace's rate limit. When push starts
# working the schedule costs almost nothing -- every message is already known,
# so each pass reads the cursor and stops.

@ext.schedule("slack_catch_up", cron="17 * * * *")
async def scheduled_catch_up(ctx):
    """Run the sweep hourly, for every connected workspace.

    Deliberately runs the SAME code path as the tool. A schedule with its own
    copy of the sweep is a second definition of "a message", and the two drift
    -- which is how a background job quietly records something different from
    what the user sees when they check by hand.

    Never raises. A scheduled task that throws is retried or disabled by the
    platform, and neither reaction helps here: the next hour's pass picks up
    whatever this one missed, because the cursor only advances on success.
    """
    try:
        result = await catch_up(ctx, CatchUpParams())
    except Exception:
        await ctx.log("Slack scheduled catch-up failed", level="warn")
        return

    # Logged at info only when something was actually learned -- an hourly
    # "nothing new" line is noise that buries the entries worth reading.
    data = getattr(result, "data", None)
    new = int(getattr(data, "messages_new", 0) or 0) if data else 0
    if new:
        await ctx.log(f"Slack catch-up recorded {new} new message(s)",
                      level="info")
