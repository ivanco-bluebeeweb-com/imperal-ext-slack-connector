"""The collection timer: a setting, and the tick that obeys it.

What these guard is not the arithmetic -- it is the two ways a timer lies. It can
claim an interval it does not keep (so the sweep runs at a speed nobody chose),
and it can lose the interval while being paused (so resuming resumes at the wrong
speed). Both are silent: nothing errors, the number in the report just stops
matching reality.
"""

import handlers_journal as hj
import sweeptimer
from models import SweepTimerParams, SweepTimerStatusParams


# --- the range ---------------------------------------------------------------

def test_the_tick_can_actually_deliver_the_fastest_offered_interval():
    """THE INVARIANT THAT HOLDS THE WHOLE DESIGN UP.

    The tick is the floor on responsiveness: nothing is awake between ticks, so
    an interval finer than the tick cannot happen. If the minimum offered
    interval were ever set below the tick rate, the app would accept "every 2
    minutes", report it, and quietly run every 5 -- a lie with no error attached.
    """
    minute_field = sweeptimer.SWEEP_TICK_CRON.split()[0]
    assert minute_field.startswith("*/"), (
        f"tick is not a step expression: {sweeptimer.SWEEP_TICK_CRON!r}")
    tick_minutes = int(minute_field[2:])

    assert tick_minutes <= sweeptimer.MIN_INTERVAL_MINUTES, (
        f"тик раз в {tick_minutes} мин не может выдержать интервал "
        f"{sweeptimer.MIN_INTERVAL_MINUTES} мин")


def test_an_interval_outside_the_range_is_pulled_into_it():
    """Clamped rather than accepted: a promise the tick cannot keep is a lie."""
    assert sweeptimer.clamp_interval(1) == sweeptimer.MIN_INTERVAL_MINUTES
    assert sweeptimer.clamp_interval(-5) == sweeptimer.MIN_INTERVAL_MINUTES
    assert sweeptimer.clamp_interval(99999) == sweeptimer.MAX_INTERVAL_MINUTES
    # In range: untouched.
    assert sweeptimer.clamp_interval(30) == 30


def test_nonsense_becomes_the_default_instead_of_crashing():
    """A bad value must not take the sweep down with it.

    This runs inside a scheduled task, where an exception is retried or the task
    is disabled -- both worse than falling back to a sane interval.
    """
    assert sweeptimer.clamp_interval("не число") == (
        sweeptimer.DEFAULT_INTERVAL_MINUTES)
    assert sweeptimer.clamp_interval(None) == sweeptimer.DEFAULT_INTERVAL_MINUTES


def test_a_clamp_is_reported_not_hidden():
    """Asking for 1 minute and getting 5 has to be SAID.

    Otherwise the user reads their own request back in a status report that
    disagrees with it, and concludes the app is broken.
    """
    assert sweeptimer._was_clamped(1) is True
    assert sweeptimer._was_clamped(99999) is True
    assert sweeptimer._was_clamped("ерунда") is True
    assert sweeptimer._was_clamped(30) is False
    # Nothing requested is not an adjustment.
    assert sweeptimer._was_clamped(None) is False


# --- whether a tick should do anything ---------------------------------------

async def test_the_very_first_tick_runs(ctx):
    """Nothing recorded means never swept -- so sweep, do not wait an interval.

    Waiting would make a fresh install silent for the length of the interval,
    which looks exactly like a broken app.
    """
    due, why = await sweeptimer.due(ctx, now=1000.0)

    assert due is True
    assert why == "first_run"


async def test_a_tick_before_the_interval_elapses_does_nothing(ctx):
    """The point of the design: a tick that is not due makes ZERO Slack calls."""
    await sweeptimer.set_interval(ctx, minutes=30)
    await sweeptimer.mark_ran(ctx, now=1000.0)

    due, why = await sweeptimer.due(ctx, now=1000.0 + 10 * 60)

    assert due is False
    assert why == "too_soon"


async def test_a_tick_after_the_interval_runs(ctx):
    await sweeptimer.set_interval(ctx, minutes=30)
    await sweeptimer.mark_ran(ctx, now=1000.0)

    due, why = await sweeptimer.due(ctx, now=1000.0 + 31 * 60)

    assert due is True
    assert why == "interval_elapsed"


async def test_a_tick_landing_just_short_still_counts(ctx):
    """Without tolerance every interval silently DOUBLES.

    Ticks do not align with the interval: a 10-minute interval checked by a
    5-minute tick lands at 9:5x, misses by seconds, and waits another full tick
    -- so "every 10 minutes" becomes every 15. This is the subtlest failure here
    and the one most likely to be dismissed as scheduler jitter.
    """
    await sweeptimer.set_interval(ctx, minutes=10)
    await sweeptimer.mark_ran(ctx, now=1000.0)

    # 9 minutes 40 seconds: a tick that arrived a hair early.
    due, why = await sweeptimer.due(ctx, now=1000.0 + 580)

    assert due is True, "интервал молча удвоился бы"
    assert why == "interval_elapsed"


async def test_pausing_stops_the_sweep(ctx):
    await sweeptimer.set_interval(ctx, paused=True)

    due, why = await sweeptimer.due(ctx, now=10_000_000.0)

    assert due is False
    assert why == "paused"


async def test_pausing_keeps_the_chosen_interval(ctx):
    """Pause must not overwrite the interval.

    If it did, resuming would resume at the default speed rather than the one
    the user picked -- and nothing about that announces itself.
    """
    await sweeptimer.set_interval(ctx, minutes=120)
    await sweeptimer.set_interval(ctx, paused=True)

    state = await sweeptimer.describe(ctx)

    assert state["paused"] is True
    assert state["interval_minutes"] == 120

    await sweeptimer.set_interval(ctx, paused=False)
    state = await sweeptimer.describe(ctx)

    assert state["paused"] is False
    assert state["interval_minutes"] == 120, "интервал потерян при паузе"


async def test_changing_the_interval_does_not_resume_a_paused_sweep(ctx):
    """The mirror image: editing the number must not silently start it again."""
    await sweeptimer.set_interval(ctx, paused=True)
    await sweeptimer.set_interval(ctx, minutes=45)

    state = await sweeptimer.describe(ctx)

    assert state["interval_minutes"] == 45
    assert state["paused"] is True, "пауза снята без просьбы"


# --- how it reads ------------------------------------------------------------

def test_the_interval_is_reported_in_words():
    """A person asked for a timer, not a cron expression."""
    assert sweeptimer.humanize_interval(5) == "каждые 5 минут"
    assert sweeptimer.humanize_interval(60) == "каждый час"
    assert sweeptimer.humanize_interval(120) == "каждые 2 часа"
    assert sweeptimer.humanize_interval(1440) == "раз в сутки"


async def test_the_next_check_is_reported_once_something_has_run(ctx):
    """The one fact a timer exists to answer."""
    await sweeptimer.set_interval(ctx, minutes=30)
    await sweeptimer.mark_ran(ctx, now=1000.0)

    state = await sweeptimer.describe(ctx)

    assert state["last_run"], "не видно, когда проверяли в последний раз"
    assert state["next_run"], "не видно, когда следующая проверка"


async def test_no_next_check_is_promised_while_paused(ctx):
    """Empty, not invented: a paused timer has no next run."""
    await sweeptimer.mark_ran(ctx, now=1000.0)
    await sweeptimer.set_interval(ctx, paused=True)

    state = await sweeptimer.describe(ctx)

    assert state["next_run"] == ""


# --- the tools ---------------------------------------------------------------

async def test_setting_the_timer_reports_the_new_rhythm(ctx):
    result = await hj.set_sweep_timer(ctx, SweepTimerParams(minutes=30))

    assert result.status == "success", result.error
    assert result.data.interval_minutes == 30
    assert result.data.interval_text == "каждые 30 минут"
    assert result.data.paused is False


async def test_an_empty_change_is_refused(ctx):
    """"Set the timer" with no value is an incomplete instruction.

    Answering it with the unchanged state would read as though something had
    been applied.
    """
    result = await hj.set_sweep_timer(ctx, SweepTimerParams())

    assert result.status == "error"
    assert "интервал" in result.error.lower()


async def test_an_out_of_range_request_is_applied_and_explained(ctx):
    """Clamped AND told, in the same breath."""
    result = await hj.set_sweep_timer(ctx, SweepTimerParams(minutes=1))

    assert result.status == "success", result.error
    assert result.data.interval_minutes == sweeptimer.MIN_INTERVAL_MINUTES
    # The summary has to admit the adjustment.
    assert "5" in result.summary


async def test_the_status_tool_reports_the_timer(ctx):
    await sweeptimer.set_interval(ctx, minutes=180)

    result = await hj.sweep_timer_status(ctx, SweepTimerStatusParams())

    assert result.status == "success", result.error
    assert result.data.interval_minutes == 180
    assert result.data.interval_text == "каждые 3 часа"
    # The tick is shown as the mechanism, never as the polling rate.
    assert result.data.tick == sweeptimer.SWEEP_TICK_CRON


# --- the tick, end to end ----------------------------------------------------

async def test_a_tick_that_is_not_due_makes_no_slack_calls(connected_ctx, http):
    """THE WHOLE ECONOMY OF THE DESIGN, asserted on the wire.

    A skipped tick must cost one store read and NOTHING on the network. If it
    still called Slack, the tick rate would be the real polling rate and every
    interval above 5 minutes would be a decorative label on the same load --
    the exact opposite of what the setting promises.

    Asserted by counting HTTP calls rather than trusting the return value,
    because "returned early" and "returned early without touching Slack" are
    different claims and only the second one matters here.
    """
    await sweeptimer.set_interval(connected_ctx, minutes=60)
    await sweeptimer.mark_ran(connected_ctx)

    before = len(http.calls)
    await hj.scheduled_catch_up(connected_ctx)

    assert len(http.calls) == before, (
        f"пропущенный тик всё равно обратился к Slack: "
        f"{[c['url'] for c in http.calls[before:]]}")


async def test_a_paused_timer_makes_no_slack_calls(connected_ctx, http):
    """Pause means pause -- including on the network."""
    await sweeptimer.set_interval(connected_ctx, paused=True)

    before = len(http.calls)
    await hj.scheduled_catch_up(connected_ctx)

    assert len(http.calls) == before


async def test_a_due_tick_records_the_attempt_before_sweeping(connected_ctx, http):
    """A failing sweep must not re-run on every tick.

    The attempt is stamped BEFORE the pass, so a Slack outage cannot turn into
    the fastest polling this app has ever done: without this, every tick would
    see "never succeeded" and try again, hammering an API that is already
    unhappy.
    """
    await sweeptimer.set_interval(connected_ctx, minutes=30)

    # No responses pushed: the sweep will fail inside. The stamp must survive.
    await hj.scheduled_catch_up(connected_ctx)

    state = await sweeptimer.describe(connected_ctx)
    assert state["last_run_at"] > 0, "неудачный проход не отметился"

    # And the next tick is therefore NOT due.
    due, why = await sweeptimer.due(connected_ctx)
    assert due is False
    assert why == "too_soon"
