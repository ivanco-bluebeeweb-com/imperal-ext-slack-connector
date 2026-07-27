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


# --- the two modes -----------------------------------------------------------
#
# MODE IS A NAME FOR `paused`, NOT A SECOND FIELD.
#
# The tempting implementation is a stored "mode" string beside the paused flag.
# It is a trap: two fields describing one fact drift, and then the report says
# ON DEMAND while the schedule is still sweeping every ten minutes -- a lie with
# no error attached, and the user only finds out from the bill. So the mode is
# DERIVED from the flag that actually gates the sweep. There is exactly one
# truth, and the name is a view of it.
#
# Naming it at all is the point of the feature: "paused: false" does not tell
# anyone they have signed up for ~4300 billable passes a month. "Автомонитор"
# does.
MODE_ON_DEMAND = "on_demand"
MODE_MONITOR = "monitor"

MODE_TEXT = {
    MODE_ON_DEMAND: "по запросу",
    MODE_MONITOR: "автомонитор",
}

#: Days used for every monthly projection. A fixed 30 rather than the real
#: length of the current month: the number exists to be COMPARED between
#: intervals, and a figure that shifts by 3% depending on whether it is February
#: makes two projections look different when only the calendar moved.
PROJECTION_DAYS = 30

#: What one billable action costs, in tokens. MUST match the per-action price
#: published for this app -- it is duplicated here because the app cannot read
#: its own Marketplace price at runtime, and a projection with no price attached
#: does not answer "what will this cost me".
#:
#: Every projection states this figure out loud instead of quietly folding it
#: into a total, so a stale constant is visible as a wrong PRICE rather than
#: hiding inside a wrong SUM.
PRICE_PER_ACTION_TOKENS = 1


def mode_of(paused: bool) -> str:
    """The mode implied by the paused flag. The ONLY place that mapping lives."""
    return MODE_ON_DEMAND if paused else MODE_MONITOR


def mode_text(mode: str) -> str:
    """Human name of a mode, falling back to the raw value rather than empty."""
    return MODE_TEXT.get(mode, mode)


def passes_per_month(interval_minutes: int) -> int:
    """How many automatic passes an interval implies over PROJECTION_DAYS.

    This is the number the whole feature exists to show. Automatic monitoring
    bills per pass, so the interval -- not the amount of Slack activity -- is
    what sets the bill: at 5 minutes it is roughly 8600 passes a month, at 6
    hours it is 120. Same app, same usefulness, seventy-fold difference in cost,
    and nothing in the UI said so before this existed.
    """
    interval = clamp_interval(interval_minutes)
    return int(PROJECTION_DAYS * 24 * 60 // interval)


def projection(interval_minutes: int) -> dict:
    """Passes and token cost per month for one interval."""
    passes = passes_per_month(interval_minutes)
    return {
        "interval_minutes": clamp_interval(interval_minutes),
        "interval_text": humanize_interval(interval_minutes),
        "passes": passes,
        "tokens": passes * PRICE_PER_ACTION_TOKENS,
    }


#: The intervals offered as a comparison table. Deliberately spans the whole
#: range: a table that stopped at an hour would hide the fact that the cheap end
#: is seventy times cheaper than the fast end.
PROJECTION_LADDER = (5, 10, 30, 60, 180, 360, 720, 1440)


def cost_ladder() -> list[dict]:
    """Every offered interval with its monthly cost, cheapest last.

    A ladder rather than a single number because "рост затрат" is a comparison:
    one figure in isolation reads as a fact of life, while the same figure next
    to a tenth of it reads as a choice.
    """
    return [projection(m) for m in PROJECTION_LADDER]


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


def compare(before: dict, after: dict) -> dict:
    """How the monthly cost moved between two states.

    Returns direction ("up" / "down" / "same") and a spoken factor, because the
    factor is the part that cannot be skimmed past: "~8640 passes" next to
    "~1440 passes" asks the reader to divide, while "в 6 раз дороже" states the
    thing the numbers were there to say.

    Handles the on-demand end honestly instead of dividing by zero: going from 0
    projected passes to thousands has no meaningful multiplier, so it is reported
    as a direction with no factor rather than an invented "infinity".
    """
    old = int(before.get("projected_passes") or 0)
    new = int(after.get("projected_passes") or 0)

    if old == new:
        return {"direction": "same", "factor": 1.0, "factor_text": ""}

    direction = "up" if new > old else "down"

    # No multiplier exists against zero. Saying so beats printing something
    # arithmetically true but meaningless.
    if not old or not new:
        return {"direction": direction, "factor": 0.0, "factor_text": ""}

    factor = (new / old) if direction == "up" else (old / new)
    return {
        "direction": direction,
        "factor": factor,
        # Whole numbers stay whole ("в 6 раз"), awkward ones get one decimal.
        # "в 6.0 раз" reads like a machine talking.
        "factor_text": _factor_text(factor),
    }


def plural(count: int, one: str, few: str, many: str) -> str:
    """A Russian count with its noun agreeing: 1 проход, 2 прохода, 5 проходов.

    Exists because these strings are read while deciding about money, and a
    machine fumbling "144 проходов" undercuts the figure it is presenting. The
    11-14 exception is the one everybody forgets (14 проходов, not 14 прохода).
    """
    last_two = abs(count) % 100
    last = abs(count) % 10
    if 11 <= last_two <= 14:
        return f"{count} {many}"
    if last == 1:
        return f"{count} {one}"
    if last in (2, 3, 4):
        return f"{count} {few}"
    return f"{count} {many}"


def passes_text(count: int) -> str:
    """"3 прохода" -- the unit this app bills in."""
    return plural(count, "проход", "прохода", "проходов")


def tokens_text(count: int) -> str:
    """"1 токен" -- the unit the user pays in."""
    return plural(count, "токен", "токена", "токенов")


def _factor_text(factor: float) -> str:
    """The multiplier in readable Russian, with the noun actually agreeing.

    This text is read while deciding about money, so "в 3 раз" -- a machine
    fumbling its own grammar -- undermines the number it is trying to convey.
    Russian needs "раз" for 5+ and "раза" for 2-4, and the rule repeats every
    ten (22 раза, 25 раз).
    """
    if abs(factor - round(factor)) >= 0.05:
        # Fractions always take "раза": в 1.3 раза.
        return f"в {factor:.1f} раза"

    whole = int(round(factor))
    last_two = whole % 100
    last = whole % 10
    if 11 <= last_two <= 14:
        noun = "раз"
    elif last in (2, 3, 4):
        noun = "раза"
    else:
        noun = "раз"
    return f"в {whole} {noun}"


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

        # THE MODE, derived -- never stored. See MODE_ON_DEMAND above for why a
        # second field would be a liability rather than a convenience.
        "mode": mode_of(paused),
        "mode_text": mode_text(mode_of(paused)),

        # WHAT THE CURRENT SETTING IMPLIES PER MONTH. Carried in the same dict as
        # the interval so no caller can report one without the other: an interval
        # shown without its cost is exactly the gap this feature exists to close.
        "projected_passes": 0 if paused else passes_per_month(interval),
        "projected_tokens": (0 if paused
                             else passes_per_month(interval)
                             * PRICE_PER_ACTION_TOKENS),

        # WHAT HAS ACTUALLY BEEN SPENT, as opposed to projected. Both are needed
        # and they answer different questions: the projection is what you are
        # signing up for, this is what already happened. A projection alone can
        # be dismissed as theory.
        "billable_passes": int(data.get("billable_passes") or 0),
        "billable_tokens": (int(data.get("billable_passes") or 0)
                            * PRICE_PER_ACTION_TOKENS),
        "counting_since": (so.humanize_ts(str(data.get("counting_since")))
                           if data.get("counting_since") else ""),
        # The raw value as well, because a write needs the number and a report
        # needs the words. Deriving one from the other is impossible in the
        # direction that matters: humanized text cannot be stored and read back.
        "counting_since_at": data.get("counting_since") or "",
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

        # The spend counter SURVIVES every settings change. Resetting it on a
        # mode switch would let a month of accrued cost vanish by toggling the
        # mode -- and a spend figure that can be erased by an unrelated action is
        # not a spend figure anyone can rely on.
        "billable_passes": current["billable_passes"],
        "counting_since": current["counting_since_at"],
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

            # COUNTED HERE, and only here. This function is called exactly once
            # per pass that actually goes to Slack -- a skipped tick never
            # reaches it -- so it is the one place where "a billable pass
            # happened" is unambiguously true. Counting in the schedule handler
            # instead would count ticks, and ticks are free.
            data["billable_passes"] = int(data.get("billable_passes") or 0) + 1
            data.setdefault("counting_since", moment)
            await ctx.store.update(SETTINGS_COLLECTION, doc.id, data)
        else:
            await ctx.store.create(SETTINGS_COLLECTION, {
                "interval_minutes": DEFAULT_INTERVAL_MINUTES,
                "paused": False,
                "last_run_at": moment,
                "billable_passes": 1,
                "counting_since": moment,
            })
    except Exception:
        # Deliberately swallowed: the sweep itself already ran. Raising here
        # would make the schedule look failed for a bookkeeping problem. The
        # cost is one early extra pass next tick, which is harmless.
        await _warn(ctx, "Slack sweep timer could not record the last run")
