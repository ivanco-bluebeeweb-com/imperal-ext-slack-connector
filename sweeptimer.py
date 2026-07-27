"""How often Slack is polled for new messages -- as a SETTING, not a constant.

WHY A TICK PLUS A STORED INTERVAL, AND NOT JUST A CRON STRING
-------------------------------------------------------------
The obvious implementation is "let the user edit the cron expression". It does
not work: the platform reads @ext.schedule(cron=...) when the app is REGISTERED,
so the cron string is fixed at deploy time. Changing the interval that way means
editing a source file and redeploying, which is exactly what this module exists
to avoid.

So the schedule becomes a TICK: it fires often, asks this module "is it time
yet?", and does nothing when the answer is no. The interval a person chooses
lives in the store, where it can change at any moment without a deploy.

THIS MAKES A LONG INTERVAL CHEAPER, NOT MORE EXPENSIVE
------------------------------------------------------
A skipped tick costs one small store read and zero Slack calls -- it never
touches the network. So "every 6 hours" genuinely polls Slack four times a day,
even though the tick wakes up every five minutes. The tick is not the poll; it
is the alarm clock next to it.

WHY THE TICK IS FIVE MINUTES
----------------------------
The tick sets the FLOOR on responsiveness: an interval finer than the tick is
unachievable, because nothing would be awake to notice. Five minutes is the
finest interval worth offering for a chat conversation, so the tick matches it.
An invariant test asserts tick <= MIN_INTERVAL_MINUTES, because that pair
silently breaking is how "every 5 minutes" would quietly become every 10.

WHY THE INTERVAL IS CLAMPED AT BOTH ENDS
----------------------------------------
Below the floor a setting would promise a speed the tick cannot deliver -- a
report that lies. Above a day the sweep stops being awareness at all: Slack's
own history call is the only way back, and a message older than a day is not
answered anyway (see autoreply.MAX_AGE_SECONDS), so a longer interval would
quietly turn auto-reply off while the status still said it was on.
"""

from __future__ import annotations

import time

import slack_objects as so


#: Where the chosen interval lives. Its own collection: this is configuration a
#: person edits, while the cursor collection is bookkeeping the sweep rewrites
#: constantly. Mixing the two would put a hand-set value in a row that machinery
#: overwrites.
SETTINGS_COLLECTION = "slack_sweep_timer"

#: The platform schedule -- the ALARM CLOCK, not the poll. Fires every five
#: minutes; each firing asks due() whether the interval has elapsed and returns
#: immediately when it has not.
SWEEP_TICK_CRON = "*/5 * * * *"

#: Kept beside the cron string it describes, so the two cannot drift: the
#: invariant test compares this against MIN_INTERVAL_MINUTES.
TICK_MINUTES = 5

MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 24 * 60
DEFAULT_INTERVAL_MINUTES = 10

#: Cron fires on wall-clock boundaries and the pass itself takes a moment, so
#: "10 minutes since the last run" is realistically 9m58s. Comparing strictly
#: would push every second tick past the mark and silently DOUBLE the effective
#: interval -- the exact bug that makes people say a timer "does not work".
TOLERANCE_SECONDS = 45


async def _warn(ctx, message: str) -> None:
    """Log a warning without ever raising (a log failure must not skip a run)."""
    try:
        await ctx.log(message, level="warn")
    except Exception:
        pass


async def _settings_doc(ctx):
    """The stored timer settings row, or None."""
    try:
        page = await ctx.store.query(SETTINGS_COLLECTION, limit=1)
    except Exception:
        return None
    rows = getattr(page, "data", None) or []
    return rows[0] if rows else None


def clamp_interval(minutes) -> int:
    """Bring any requested interval inside the achievable range.

    Clamps rather than rejects: someone asking for "every minute" wants the
    fastest available, and refusing the whole call teaches them nothing. The
    caller reports what was actually applied, so a clamp is never silent.
    """
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MINUTES
    return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, value))


def _was_clamped(minutes) -> bool:
    """Whether a requested interval had to be adjusted to fit the range.

    Kept beside clamp_interval so the two cannot answer differently. A separate
    helper rather than an expression at the call site because the interesting
    case is the awkward one: an unparseable value IS adjusted (it becomes the
    default), and an inline comparison of a non-number against a number is how
    that case turns into a crash instead of an honest "yes, adjusted".
    """
    if minutes is None:
        return False
    try:
        return int(minutes) != clamp_interval(minutes)
    except (TypeError, ValueError):
        return True


def humanize_interval(minutes: int) -> str:
    """'каждые 10 минут' / 'каждый час' / 'каждые 6 часов'.

    Words, because the interval is reported to a person. A bare number of
    minutes is readable at 10 and unreadable at 360.
    """
    value = int(minutes)
    if value % 60 == 0:
        hours = value // 60
        if hours == 1:
            return "каждый час"
        if hours == 24:
            return "раз в сутки"
        if 2 <= hours <= 4:
            return f"каждые {hours} часа"
        return f"каждые {hours} часов"
    if value == 1:
        return "каждую минуту"
    if value in (2, 3, 4):
        return f"каждые {value} минуты"
    return f"каждые {value} минут"


def _next_run_text(last_run, interval_minutes: int, paused: bool) -> str:
    """When the next check falls due, in words. Empty when there is no answer.

    Deliberately empty rather than "now" in two cases: while paused there is no
    next run at all, and before the first run the sweep is already due -- naming
    a future moment for something about to happen immediately would misinform.
    """
    if paused or not last_run:
        return ""
    try:
        due_at = float(last_run) + interval_minutes * 60
    except (TypeError, ValueError):
        return ""
    return so.humanize_ts(str(due_at))


async def describe(ctx) -> dict:
    """The timer as a person should see it: interval, paused, last run.

    Falls back to the default interval when nothing is stored, and says so via
    `configured`. "Never set" and "set to the same value as the default" look
    identical in the data but are different answers to "did anyone choose this?".
    """
    doc = await _settings_doc(ctx)
    data = (getattr(doc, "data", None) or {}) if doc is not None else {}

    configured = bool(data)
    interval = clamp_interval(data.get("interval_minutes",
                                       DEFAULT_INTERVAL_MINUTES))
    paused = bool(data.get("paused"))
    last_run = data.get("last_run_at")
    changed_at = data.get("changed_at")

    return {
        "interval_minutes": interval,
        "interval_text": humanize_interval(interval),
        "paused": paused,
        "configured": configured,
        "last_run_at": float(last_run) if last_run else 0.0,
        # Words rather than an epoch float, through the same humanizer the
        # journal uses so two places never disagree about how a time is written.
        "last_run_text": so.humanize_ts(str(last_run)) if last_run else "",
        "changed_at_text": so.humanize_ts(str(changed_at)) if changed_at else "",
        "note": str(data.get("note") or ""),
        "tick": SWEEP_TICK_CRON,

        # Names the handlers actually read. Kept as aliases rather than renaming
        # the pair above, because "last_run_at" (a number, for arithmetic) and
        # "last_run" (words, for a human) are genuinely two different things and
        # collapsing them is how a report ends up printing an epoch float.
        "last_run": so.humanize_ts(str(last_run)) if last_run else "",

        # WHEN THE NEXT CHECK IS DUE -- the one fact a person actually wants from
        # a timer, and the one this returned nothing for. Empty while paused (no
        # next run exists) and empty before the first run (it is due now, and
        # inventing a future time for "immediately" would be a lie).
        "next_run": _next_run_text(last_run, interval, paused),
    }


async def set_interval(ctx, *, minutes=None, paused=None,
                       note: str = "") -> dict:
    """Change the interval and/or pause collection. Returns the applied state.

    Both arguments are optional and None means "leave as it is": pausing must
    not require restating the interval, and changing the interval must not
    silently resume a paused sweep. A single call that always wrote both fields
    would make "pause" and "set to 30 minutes" interfere with each other.

    Reports `saved` so the caller can tell a refused write from a successful
    one. Claiming success on a failed write is the worst outcome here: the user
    believes Slack is being polled every 5 minutes when nothing changed.
    """
    current = await describe(ctx)
    interval = (current["interval_minutes"] if minutes is None
                else clamp_interval(minutes))
    is_paused = current["paused"] if paused is None else bool(paused)

    data = {
        "interval_minutes": interval,
        "paused": is_paused,
        "changed_at": time.time(),
        "note": note or current["note"],
        # Preserved deliberately: forgetting the last run would make the very
        # next tick look overdue and fire immediately, so every settings edit
        # would trigger an unscheduled poll.
        "last_run_at": current["last_run_at"] or "",
    }

    doc = await _settings_doc(ctx)
    saved = True
    try:
        if doc is not None and getattr(doc, "id", ""):
            await ctx.store.update(SETTINGS_COLLECTION, doc.id, data)
        else:
            await ctx.store.create(SETTINGS_COLLECTION, data)
    except Exception:
        saved = False
        await _warn(ctx, "Slack sweep timer could not be saved")

    return {
        "saved": saved,
        "interval_minutes": interval,
        "interval_text": humanize_interval(interval),
        "paused": is_paused,
        # Whether the request was adjusted, so a clamp is never a surprise:
        # asking for 1 minute and getting 5 has to be SAID, not discovered later
        # from a status report that disagrees with what you typed.
        "clamped": _was_clamped(minutes),
    }


async def due(ctx, *, now: float | None = None) -> tuple[bool, str]:
    """Whether this tick should actually sweep. Returns (yes/no, why).

    Fails towards RUNNING. If the settings row cannot be read, the sweep still
    happens: an unreadable setting must not silently stop Webbee from seeing
    Slack -- and one extra poll is a far cheaper mistake than an app that went
    quietly deaf.
    """
    moment = now if now is not None else time.time()
    state = await describe(ctx)

    if state["paused"]:
        return False, "paused"

    last = state["last_run_at"]
    if not last:
        return True, "first_run"

    elapsed = moment - last
    if elapsed < 0:
        # Clock moved backwards (NTP correction, restore from backup). Treating
        # a negative gap as "not due" would wedge the sweep until wall-clock
        # time caught up, which can be hours.
        return True, "clock_moved"

    needed = state["interval_minutes"] * 60 - TOLERANCE_SECONDS
    if elapsed >= needed:
        return True, "interval_elapsed"
    return False, "too_soon"


async def mark_ran(ctx, *, now: float | None = None) -> None:
    """Record that a sweep pass happened, so the interval is measured from it.

    Written on every ATTEMPT, not only on success. The interval is a rate limit
    on how often we poll somebody else's API; letting failures reset it would
    turn a Slack outage into the fastest polling this app ever does, at exactly
    the moment Slack least wants the traffic. The cursor only advances on
    success, so a missed pass loses nothing but time.
    """
    moment = now if now is not None else time.time()
    doc = await _settings_doc(ctx)
    try:
        if doc is not None and getattr(doc, "id", ""):
            data = dict(getattr(doc, "data", None) or {})
            data["last_run_at"] = moment
            await ctx.store.update(SETTINGS_COLLECTION, doc.id, data)
        else:
            await ctx.store.create(SETTINGS_COLLECTION, {
                "interval_minutes": DEFAULT_INTERVAL_MINUTES,
                "paused": False,
                "last_run_at": moment,
            })
    except Exception:
        # Deliberately swallowed: the sweep itself already ran. Raising here
        # would make the schedule look failed for a bookkeeping problem. The
        # cost is one early extra pass next tick, which is harmless.
        await _warn(ctx, "Slack sweep timer could not record the last run")
