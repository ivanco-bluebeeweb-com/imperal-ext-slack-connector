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
import autoreply
import inbound
import journal
import sweeptimer
import shared
import slack_client as sc
import slack_objects as so
from app import chat, ext
from models import (
    AppModeParams,
    AppModeStatus,
    AppModeStatusParams,
    AutoReplyParams,
    AutoReplyStatus,
    AutoReplyStatusParams,
    SweepTimerParams,
    SweepTimerStatus,
    SweepTimerStatusParams,
    JoinChannelsParams,
    JoinReport,
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
    """One journal row as the entity the caller sees.

    `posted_at` is RE-DERIVED from the stored ts rather than read back
    verbatim. Rows written before the date format was fixed hold the old
    "2026-07-27 18:28" shape, which the platform's PII guard redacts to
    "<PHONE>:28" -- so history would stay unreadable in chat forever while new
    messages rendered fine. Deriving it on read fixes the backlog without a
    data migration, and costs nothing: ts is already the source of truth.

    The raw `ts` fields are passed through UNTOUCHED. They are the message's
    identity and go back to Slack when replying; reformatting them would break
    replies, which is a far worse failure than an ugly date.
    """
    message_ts = str(row.get("message_ts") or "")
    return InboundMessage(
        text=str(row.get("text_readable") or row.get("text") or ""),
        author=str(row.get("user_display_name") or row.get("user_id") or ""),
        author_id=str(row.get("user_id") or ""),
        channel=str(row.get("channel_name") or row.get("channel_id") or ""),
        channel_id=str(row.get("channel_id") or ""),
        is_dm=bool(row.get("is_dm")),
        ts=message_ts,
        posted_at=(so.humanize_ts(message_ts)
                   or str(row.get("posted_at") or "")),
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
    # Tracked separately from the skip LIST because the advice depends on the
    # CAUSE. Telling someone to /invite the app into a Slackbot DM -- which is
    # what a blanket note did -- is the same class of wrong guidance as the DM
    # advice this connector already had to correct: confidently actionable, and
    # impossible to act on.
    joinable_skips: list[str] = []
    for conv in conversations:
        ok, why = journal.is_reachable(conv)
        if ok:
            reachable.append(conv)
        else:
            skipped.append(f"{so.channel_name(conv)} ({why})")
            if not conv.get("is_im") and not conv.get("is_mpim"):
                joinable_skips.append(so.channel_name(conv))

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
        note = ""
        if joinable_skips:
            # Only for channels, and it names the tool rather than the chore:
            # public channels the app can join itself, private ones genuinely
            # need a human inside.
            note = (" Channels the app is not in can be joined with "
                    "join_channels (public), or by /invite @imperal from "
                    "inside a private channel.")
        lines.append(
            f"Not swept ({len(skipped)}): " + "; ".join(skipped[:8]) + "."
            + note)

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
# moment of asking. A schedule is what turns the journal from an archive you
# consult into awareness that keeps itself current, and it needs neither the
# signing secret nor an automations slot.
#
# THE CRON HERE IS A TICK, NOT THE POLLING INTERVAL.
#
# The platform reads this decorator when the app is REGISTERED, so the cron
# string is fixed at deploy time -- which means "let the user change how often
# Slack is checked" cannot be done by editing it. Instead the schedule fires
# often and cheaply, and sweeptimer.due() decides whether the chosen interval
# has actually elapsed. A tick that is not due returns after one small store
# read, having made ZERO Slack calls, so a long interval is genuinely cheaper
# than a short one even though the tick rate never changes.
#
# The interval itself is a setting (see sweeptimer): changing it takes effect on
# the next tick, with no deploy.

@ext.schedule("slack_catch_up", cron=sweeptimer.SWEEP_TICK_CRON)
async def scheduled_catch_up(ctx):
    """Sweep when the chosen interval says so, for every connected workspace.

    Deliberately runs the SAME code path as the tool. A schedule with its own
    copy of the sweep is a second definition of "a message", and the two drift
    -- which is how a background job quietly records something different from
    what the user sees when they check by hand.

    Never raises. A scheduled task that throws is retried or disabled by the
    platform, and neither reaction helps here: the next pass picks up whatever
    this one missed, because the cursor only advances on success.
    """
    # THE GATE. Checked before anything else: a tick that is not due must not
    # touch Slack, or the setting would be decorative.
    ready, why = await sweeptimer.due(ctx)
    if not ready:
        return

    # Recorded BEFORE the pass, not after. If the sweep is slow or throws, the
    # interval is still measured from when this attempt began -- otherwise a
    # failing pass would re-run on every single tick, turning a Slack outage
    # into the fastest polling this app has ever done.
    await sweeptimer.mark_ran(ctx)

    if why == "clock_moved":
        await ctx.log("Slack sweep timer saw the clock move backwards; "
                      "sweeping now", level="warn")

    try:
        result = await catch_up(ctx, CatchUpParams())
    except Exception:
        await ctx.log("Slack scheduled catch-up failed", level="warn")
        return

    # Logged at info only when something was actually learned -- a recurring
    # "nothing new" line is noise that buries the entries worth reading.
    data = getattr(result, "data", None)
    new = int(getattr(data, "messages_new", 0) or 0) if data else 0
    if new:
        await ctx.log(f"Slack catch-up recorded {new} new message(s)",
                      level="info")

    # ANSWERING RIDES THE SAME PASS.
    #
    # This is the moment new messages become known, so it is the moment to
    # answer them -- no second schedule to drift out of step with the first,
    # and no automations slot spent on "read my own journal".
    #
    # In its own try/except, and deliberately AFTER the sweep: collecting
    # messages is the job this schedule was built for, and a failure in the
    # newer, chattier half must not cost the workspace its awareness. Silence
    # is the correct outcome when auto-reply is off, which is the default.
    try:
        report = await autoreply.run_once(ctx)
    except Exception:
        await ctx.log("Slack auto-reply pass failed", level="warn")
        return

    if report.get("replied"):
        await ctx.log(
            f"Slack auto-reply answered {report['replied']} message(s)",
            level="info")
    elif report.get("skipped"):
        await ctx.log(
            f"Slack auto-reply skipped {report['skipped']} message(s): "
            f"{report.get('detail') or report.get('reason') or 'причина не указана'}",
            level="warn")


# --- joining channels, so awareness does not need a human in every one -------

@chat.function(
    "join_channels",
    "Have the Slack app add itself to public channels, so it can see messages "
    "there. Names the channels, or joins every public channel it is not yet in. "
    "Private channels still need an invite from someone inside.",
    action_type="write", chain_callable=True,
    effects=["slack.channel.joined"],
    event="slack-connector.join_channels",
    data_model=JoinReport,
)
async def join_channels(ctx, params: JoinChannelsParams) -> ActionResult:
    """Join public channels with conversations.join.

    WHY THIS IS A WRITE AND STILL SAFE. It changes the workspace -- the app
    appears as a member, and Slack posts a visible "added to the channel" line.
    That is real, so it is action_type="write" and never runs by itself: no
    schedule calls it. But it cannot read anything the user has not already
    made public, and it cannot touch private channels at all.

    It reports THREE outcomes separately -- joined, already in, and could not --
    because "nothing happened" has two very different meanings. Already-in is
    success; a scope refusal is not, and collapsing them into one number is how a
    missing scope hides behind a cheerful summary.
    """
    token, workspace, err = await _resolve(ctx, params.workspace)
    if err:
        return err

    listing = await sc.paginate(
        ctx, "GET", "conversations.list", token,
        params={"types": "public_channel", "exclude_archived": True},
        results_key="channels", limit=1000)
    if not listing.get("ok"):
        return _from_envelope(listing)

    visible = [c for c in (listing.get("results") or []) if isinstance(c, dict)]
    by_name = {str(c.get("name") or "").lower(): c for c in visible}
    by_id = {str(c.get("id") or ""): c for c in visible}

    wanted: list[dict] = []
    unknown: list[str] = []
    if params.channels.strip():
        for ref in params.channels.split(","):
            ref = ref.strip()
            if not ref:
                continue
            key = so.normalize_channel_ref(ref)
            found = by_id.get(key) or by_name.get(key.lower().lstrip("#"))
            if found is None:
                unknown.append(ref)
            else:
                wanted.append(found)
    else:
        # Every public channel not already joined. This is the case that makes
        # the tool worth having: one call instead of one /invite per channel.
        wanted = [c for c in visible if not c.get("is_member")]

    already = [c for c in wanted if c.get("is_member")]
    todo = [c for c in wanted if not c.get("is_member")]

    if params.dry_run:
        names = ", ".join("#" + str(c.get("name") or c.get("id")) for c in todo)
        return ActionResult.success(
            summary=(f"Would join {len(todo)} channel(s): {names}."
                     if todo else "Nothing to join — already in every public "
                                  "channel it can see."),
            data=JoinReport(
                joined="", joined_count=0,
                already_in=", ".join("#" + str(c.get("name") or "")
                                     for c in already),
                already_count=len(already),
                needs_a_human=", ".join(unknown),
                state="dry run",
                detail=f"Would join: {names}" if names else "Nothing to join"))

    joined: list[str] = []
    failed: list[str] = []
    for conv in todo:
        label = "#" + str(conv.get("name") or conv.get("id"))
        out = await sc.request(ctx, "POST", "conversations.join", token,
                               json={"channel": conv.get("id")})
        if out.get("ok"):
            joined.append(label)
        else:
            # The Slack error is kept per channel: one channel refusing (say,
            # an admin-restricted channel) must not hide the ones that worked.
            failed.append(f"{label} ({out.get('code') or 'refused'})")

    parts: list[str] = []
    if joined:
        parts.append(f"Joined {len(joined)}: {', '.join(joined)}.")
    if already:
        parts.append(f"Already in {len(already)}.")
    if failed:
        parts.append(f"Could not join {len(failed)}: {', '.join(failed)}.")
    if unknown:
        parts.append(
            f"Not found as a public channel: {', '.join(unknown)} — a private "
            "channel cannot be self-joined; someone inside must "
            "/invite @imperal.")
    if not parts:
        parts.append("Nothing to do — already in every public channel it can "
                     "see.")

    if joined:
        parts.append("Run catch_up to read what was said in them.")

    return ActionResult.success(
        summary=" ".join(parts),
        data=JoinReport(
            joined=", ".join(joined), joined_count=len(joined),
            already_in=", ".join("#" + str(c.get("name") or "")
                                 for c in already),
            already_count=len(already),
            failed=", ".join(failed), failed_count=len(failed),
            needs_a_human=", ".join(unknown),
            state=(f"joined {len(joined)}" if joined
                   else "nothing to join" if not failed else "refused"),
            detail=" ".join(parts)))


# --- answering by herself ----------------------------------------------------
#
# A switch, deliberately. Automatic answering is the one capability here that
# writes to OTHER people's Slack without anyone watching, so it does not arrive
# switched on and it is not inferred from context -- somebody says yes.


@chat.function(
    "set_autoreply",
    "Turn automatic answering on or off: whether Webbee replies by herself "
    "when someone mentions her in Slack or writes her a DM.",
    action_type="write", chain_callable=True,
    effects=["slack.autoreply.changed"],
    event="slack-connector.set_autoreply",
    data_model=AutoReplyStatus,
)
async def set_autoreply(ctx, params: AutoReplyParams) -> ActionResult:
    """Flip the auto-reply switch and report the resulting state.

    Reports what is WAITING as well as the new state, because "on" with three
    messages already pending means three replies go out on the next pass -- and
    that is worth knowing at the moment of switching, not afterwards.
    """
    saved = await autoreply.set_enabled(ctx, params.enabled, note=params.note)
    if not saved:
        return _error(
            "Настройку автоответов не удалось сохранить. Попробуй ещё раз.",
            sc.SLACK_SETTING_WRITE_FAILED)

    waiting = len(await autoreply.pending(ctx))
    # The reply latency a person actually experiences is the SWEEP interval, not
    # anything auto-reply owns: replies ride the sweep. Reporting a hardcoded
    # figure here is how a status line ends up contradicting the timer.
    timer_state = await sweeptimer.describe(ctx)
    if params.enabled:
        detail = "Автоответы включены"
        summary = (f"Автоответы включены. Ждут ответа: {waiting}."
                   if waiting else
                   "Автоответы включены. Сейчас ничего не ждёт ответа.")
    else:
        detail = "Автоответы выключены"
        summary = ("Автоответы выключены — Webbee больше не отвечает в Slack "
                   "сама.")

    return ActionResult.success(
        summary=summary,
        data=AutoReplyStatus(
            enabled=params.enabled, waiting=waiting,
            schedule=timer_state["interval_text"],
            max_per_pass=autoreply.MAX_REPLIES_PER_RUN,
            changed_at=(await autoreply.describe(ctx))["changed_at"],
            note=params.note, detail=detail))


@chat.function(
    "autoreply_status",
    "Report whether Webbee answers Slack messages by herself, how many "
    "messages are waiting for an answer, and when the next pass runs.",
    action_type="read", chain_callable=True,
    data_model=AutoReplyStatus,
)
async def autoreply_status(ctx, params: AutoReplyStatusParams) -> ActionResult:
    """State of automatic answering, and what it would do next."""
    state = await autoreply.describe(ctx)
    timer_state = await sweeptimer.describe(ctx)
    enabled = state["enabled"]
    waiting_rows = await autoreply.pending(ctx)
    waiting = len(waiting_rows)

    if not enabled:
        detail = "Автоответы выключены"
        summary = ("Автоответы выключены: Webbee видит обращения, но не "
                   "отвечает сама.")
        if waiting:
            summary += f" Ждут ответа: {waiting}."
    elif waiting:
        detail = f"Автоответы включены, ждут ответа: {waiting}"
        summary = (f"Автоответы включены. Ждут ответа: {waiting} — ответы "
                   f"уйдут на следующем проходе.")
    else:
        detail = "Автоответы включены, всё отвечено"
        summary = "Автоответы включены. Всё, к чему обращались, уже отвечено."

    return ActionResult.success(
        summary=summary,
        data=AutoReplyStatus(
            enabled=enabled, waiting=waiting,
            schedule=timer_state["interval_text"],
            max_per_pass=autoreply.MAX_REPLIES_PER_RUN,
            changed_at=state["changed_at"], note=state["note"],
            detail=detail))


# --- the collection timer, as a setting --------------------------------------
# Until this existed the interval was a constant: changing it meant editing a
# source file and redeploying, which is not something a user can do. The value
# is now stored, and these two functions are how it is read and written.

@chat.function(
    "set_sweep_timer",
    "Change how often Webbee checks Slack for new messages (5 minutes to 24 "
    "hours), or pause scheduled checking.",
    action_type="write", chain_callable=True,
    effects=["slack.sweep_timer.changed"],
    event="slack-connector.set_sweep_timer",
    data_model=SweepTimerStatus,
)
async def set_sweep_timer(ctx, params: SweepTimerParams) -> ActionResult:
    """Set the interval, the paused flag, or both.

    Refuses a call that asks for nothing rather than reporting a cheerful
    success: "set the timer" with no value is an incomplete instruction, and
    answering it with the unchanged state reads as though something was applied.
    """
    if params.minutes is None and params.paused is None:
        return ActionResult.error(
            "Скажи, что поменять: интервал в минутах (от 5 до 1440) "
            "или пауза.",
            code=sc.SLACK_VALIDATION_FAILED)

    outcome = await sweeptimer.set_interval(
        ctx, minutes=params.minutes, paused=params.paused)

    if not outcome["saved"]:
        return ActionResult.error(
            "Не удалось сохранить настройку таймера. Попробуй ещё раз.",
            code=sc.SLACK_SETTING_WRITE_FAILED)

    state = await sweeptimer.describe(ctx)
    reply_note = (" Автоответы включены, значит и они пойдут в этом ритме."
                  if await autoreply.is_enabled(ctx) else "")

    if state["paused"]:
        summary = ("Плановая проверка Slack на паузе. Интервал сохранён "
                   f"({state['interval_text']}) — вернётся при возобновлении. "
                   "Проверить вручную можно в любой момент.")
    else:
        summary = f"Теперь Webbee проверяет Slack {state['interval_text']}."
        if outcome["clamped"]:
            # Said out loud: asking for 1 minute and silently getting 5 is a
            # setting that disagrees with what the user typed.
            summary += (" Запрошенное значение вне допустимого диапазона "
                        f"({sweeptimer.MIN_INTERVAL_MINUTES}–"
                        f"{sweeptimer.MAX_INTERVAL_MINUTES} мин), "
                        "поэтому взято ближайшее возможное.")
        summary += reply_note

    return ActionResult.success(summary=summary, data=_timer_entity(state))


@chat.function(
    "sweep_timer_status",
    "Report how often Webbee checks Slack for new messages, whether checking "
    "is paused, and when the next check is due.",
    action_type="read", chain_callable=True,
    data_model=SweepTimerStatus,
)
async def sweep_timer_status(ctx, params: SweepTimerStatusParams) -> ActionResult:
    """What the timer is set to, and when it next fires."""
    state = await sweeptimer.describe(ctx)

    if state["paused"]:
        summary = ("Плановая проверка Slack на паузе. Сохранённый интервал: "
                   f"{state['interval_text']}.")
    else:
        summary = f"Webbee проверяет Slack {state['interval_text']}."
        if state["next_run"]:
            summary += f" Следующая проверка: {state['next_run']}."

    return ActionResult.success(summary=summary, data=_timer_entity(state))


def _timer_entity(state: dict) -> SweepTimerStatus:
    """One place that shapes the timer entity, so the two tools cannot drift."""
    title = (f"Проверка Slack на паузе ({state['interval_text']})"
             if state["paused"]
             else f"Проверка Slack: {state['interval_text']}")
    return SweepTimerStatus(
        interval_minutes=state["interval_minutes"],
        interval_text=state["interval_text"],
        paused=state["paused"],
        tick=sweeptimer.SWEEP_TICK_CRON,
        last_run=state["last_run"],
        next_run=state["next_run"],
        detail=title,
        title=title)


# --- the two modes, and what each one costs ----------------------------------
#
# The app has always had these two states -- they were just called `paused` and
# reported as a boolean nobody could price. Naming them, and attaching the cost
# of each to the moment of choosing, is the whole point: "paused: false" does not
# tell anyone they have signed up for thousands of billable passes a month.
#
# Cost is stated at every point where it CHANGES (switching to monitor, changing
# the interval) rather than only in a status anyone has to think to ask for. A
# cost you have to go looking for is a cost you discover on the bill.

@chat.function(
    "set_mode",
    "Switch between the two modes: 'monitor' (Webbee checks Slack "
    "automatically on a timer, billed per pass) and 'on_demand' (she reads "
    "Slack only when asked, no automatic charges).",
    action_type="write", chain_callable=True,
    effects=["slack.mode.changed"],
    event="slack-connector.set_mode",
    data_model=AppModeStatus,
)
async def set_mode(ctx, params: AppModeParams) -> ActionResult:
    """Pick a mode, and say what it will cost before anything runs.

    Refuses an unrecognised mode instead of guessing. One of these two states
    spends money on a schedule; picking the expensive one from an ambiguous word
    is not a mistake that can be undone by switching back, because the passes
    have already been billed.
    """
    wanted = (params.mode or "").strip().lower()

    aliases = {
        "monitor": sweeptimer.MODE_MONITOR,
        "auto": sweeptimer.MODE_MONITOR,
        "автомонитор": sweeptimer.MODE_MONITOR,
        "on_demand": sweeptimer.MODE_ON_DEMAND,
        "on demand": sweeptimer.MODE_ON_DEMAND,
        "ondemand": sweeptimer.MODE_ON_DEMAND,
        "manual": sweeptimer.MODE_ON_DEMAND,
        "по запросу": sweeptimer.MODE_ON_DEMAND,
    }
    mode = aliases.get(wanted, "")
    if not mode:
        return ActionResult.error(
            "Режим бывает двух видов: 'monitor' — Webbee сама проверяет Slack "
            "по таймеру (платно за проход), или 'on_demand' — читает только "
            "когда попросишь (без автоматических трат).",
            code=sc.SLACK_VALIDATION_FAILED)

    # READ BEFORE WRITING. The point of this whole feature is that a change in
    # spending is VISIBLE, and "visible" means stated as a change -- x6 -- not
    # left as two numbers for the user to divide in their head. That comparison
    # is impossible after the write, so the old state is captured first.
    before = await sweeptimer.describe(ctx)

    # Monitor mode IS "not paused" -- one stored truth, named two ways.
    outcome = await sweeptimer.set_interval(
        ctx, minutes=params.minutes,
        paused=(mode == sweeptimer.MODE_ON_DEMAND))

    if not outcome["saved"]:
        return ActionResult.error(
            "Не удалось сохранить режим. Попробуй ещё раз.",
            code=sc.SLACK_SETTING_WRITE_FAILED)

    state = await sweeptimer.describe(ctx)
    autoreply_on = await autoreply.is_enabled(ctx)

    if state["mode"] == sweeptimer.MODE_MONITOR:
        detail = (f"Автомонитор: {state['interval_text']}, "
                  f"~{state['projected_passes']} проходов в месяц")
        summary = (
            f"Режим: автомонитор — Webbee сама проверяет Slack "
            f"{state['interval_text']}. "
            f"Это ~{state['projected_passes']} оплачиваемых проходов в месяц "
            f"(~{state['projected_tokens']} токенов при цене "
            f"{sweeptimer.PRICE_PER_ACTION_TOKENS} за проход).")
        if autoreply_on:
            summary += " Автоответы включены — они идут в этом же ритме."
        # THE CHANGE, SPOKEN AS A CHANGE. Two numbers side by side make the
        # reader do the arithmetic; "в 6 раз больше" cannot be skimmed past.
        # Stated in whichever direction it went -- hiding a DECREASE would be
        # the same failure of nerve as hiding an increase, and would make this
        # line read as scaremongering rather than information.
        movement = sweeptimer.compare(before, state)

        # THE ZERO CASE IS THE COMMON ONE, and it has no multiplier: coming from
        # on-demand there were no automatic passes to multiply. It gets its own
        # sentence because the template that says "в N раз дороже" produced
        # "Это  дороже" here -- a hole exactly where the warning matters most,
        # on the switch that STARTS automatic spending.
        if movement["direction"] == "up" and not movement["factor_text"]:
            summary += (f" ⚠️ Раньше автоматических трат не было — теперь это "
                        f"~{sweeptimer.passes_text(state['projected_passes'])} "
                        f"в месяц.")
        elif movement["direction"] == "up":
            summary += (f" ⚠️ Это {movement['factor_text']} дороже, чем было "
                        f"({before['mode_text']}, "
                        f"~{sweeptimer.passes_text(before['projected_passes'])}"
                        f").")
        elif movement["direction"] == "down" and movement["factor_text"]:
            summary += (f" Это {movement['factor_text']} дешевле, чем было "
                        f"(~{sweeptimer.passes_text(before['projected_passes'])}"
                        f").")

        # And a cheaper alternative, so the figure reads as a choice rather than
        # a fact of life.
        cheaper = sweeptimer.projection(
            min(sweeptimer.MAX_INTERVAL_MINUTES,
                state["interval_minutes"] * 6))
        if cheaper["passes"] < state["projected_passes"]:
            summary += (f" Для сравнения: {cheaper['interval_text']} — "
                        f"~{cheaper['passes']} проходов "
                        f"(~{cheaper['tokens']} токенов).")
    else:
        detail = "По запросу: автоматических проходов нет"
        summary = (
            "Режим: по запросу — Webbee читает Slack только когда попросишь. "
            "Автоматических проходов и автоматических трат нет. "
            f"Интервал сохранён ({state['interval_text']}) на случай "
            "возврата в автомонитор.")
        if before["projected_passes"]:
            summary += (f" Экономия: ~{before['projected_passes']} проходов в "
                        f"месяц (~{before['projected_tokens']} токенов) больше "
                        f"не тратятся.")
        if autoreply_on:
            summary += (" Автоответы включены, но сработают только при "
                        "проверке — то есть когда попросишь.")

    if outcome["clamped"]:
        summary += (f" Интервал поправлен до допустимого диапазона "
                    f"({sweeptimer.MIN_INTERVAL_MINUTES}–"
                    f"{sweeptimer.MAX_INTERVAL_MINUTES} мин).")

    return ActionResult.success(
        summary=summary,
        data=AppModeStatus(
            mode=state["mode"], mode_text=state["mode_text"],
            interval_minutes=state["interval_minutes"],
            interval_text=state["interval_text"],
            projected_passes=state["projected_passes"],
            projected_tokens=state["projected_tokens"],
            billable_passes=state["billable_passes"],
            billable_tokens=state["billable_tokens"],
            counting_since=state["counting_since"],
            price_per_action=sweeptimer.PRICE_PER_ACTION_TOKENS,
            autoreply_enabled=autoreply_on,
            detail=detail))


@chat.function(
    "mode_status",
    "Report which mode the app is in, what it has cost so far, and what each "
    "monitoring interval would cost per month.",
    action_type="read", chain_callable=True,
    data_model=AppModeStatus,
)
async def mode_status(ctx, params: AppModeStatusParams) -> ActionResult:
    """Mode, actual spend, projected spend, and the comparison ladder.

    Shows spent AND projected because they are dismissible alone: a projection
    reads as theory until something has actually been billed, and a total reads
    as a fact of life until you can see the cheaper option beside it.
    """
    state = await sweeptimer.describe(ctx)
    autoreply_on = await autoreply.is_enabled(ctx)
    price = sweeptimer.PRICE_PER_ACTION_TOKENS

    lines = []
    if state["mode"] == sweeptimer.MODE_MONITOR:
        detail = (f"Автомонитор: {state['interval_text']}, "
                  f"~{state['projected_passes']} проходов в месяц")
        lines.append(
            f"Режим: АВТОМОНИТОР — Webbee проверяет Slack "
            f"{state['interval_text']} сама.")
        lines.append(
            f"Прогноз: ~{state['projected_passes']} оплачиваемых проходов в "
            f"месяц (~{state['projected_tokens']} токенов).")
    else:
        detail = "По запросу: автоматических проходов нет"
        lines.append(
            "Режим: ПО ЗАПРОСУ — Webbee читает Slack только когда попросишь.")
        lines.append(
            "Прогноз: 0 автоматических проходов, 0 токенов в месяц. "
            f"Интервал сохранён ({state['interval_text']}) на случай возврата "
            "в автомонитор.")

    # Actual spend before projections: what already happened is the harder fact.
    if state["billable_passes"]:
        lines.append(
            f"Фактически потрачено: "
            f"{sweeptimer.passes_text(state['billable_passes'])} "
            f"= {sweeptimer.tokens_text(state['billable_tokens'])}"
            + (f" (учёт с {state['counting_since']})"
               if state["counting_since"] else ""))
    else:
        lines.append("Фактически потрачено: пока ни одного платного прохода.")

    lines.append(f"Цена: {sweeptimer.tokens_text(price)} за проход. "
                 "Ручные запросы тарифицируются как обычное действие.")
    lines.append("")
    lines.append("Сколько стоит автомонитор при разных интервалах (в месяц):")
    for row in sweeptimer.cost_ladder():
        marker = ("  ← сейчас"
                  if (row["interval_minutes"] == state["interval_minutes"]
                      and state["mode"] == sweeptimer.MODE_MONITOR)
                  else "")
        lines.append(f"  {row['interval_text']}: ~{row['passes']} проходов "
                     f"= ~{row['tokens']} токенов{marker}")

    if autoreply_on:
        lines.append("")
        lines.append("Автоответы включены: они идут внутри тех же проходов и "
                     "отдельно не тарифицируются.")

    return ActionResult.success(
        summary="\n".join(lines),
        data=AppModeStatus(
            mode=state["mode"], mode_text=state["mode_text"],
            interval_minutes=state["interval_minutes"],
            interval_text=state["interval_text"],
            projected_passes=state["projected_passes"],
            projected_tokens=state["projected_tokens"],
            billable_passes=state["billable_passes"],
            billable_tokens=state["billable_tokens"],
            counting_since=state["counting_since"],
            price_per_action=price,
            autoreply_enabled=autoreply_on,
            detail=detail))
