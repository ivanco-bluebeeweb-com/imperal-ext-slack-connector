"""The two modes, and whether the cost of each is told truthfully.

WHAT IS ACTUALLY AT RISK HERE
-----------------------------
This feature exists so that spending is visible BEFORE it happens. Every failure
mode below is silent -- nothing errors, the app just bills differently from what
it said:

  * a stored mode drifting from the flag that gates the sweep (report says ON
    DEMAND, schedule keeps sweeping),
  * a projection that does not match the interval actually in effect,
  * counting ticks instead of passes (free wake-ups billed as work),
  * a change in cost stated as two numbers instead of as a change.

The user only discovers any of these from the bill, which is why they are tested
rather than eyeballed.
"""

import handlers_journal as hj
import sweeptimer
from models import AppModeParams, AppModeStatusParams


# --- one truth, two names ----------------------------------------------------

def test_the_mode_is_derived_from_the_flag_that_gates_the_sweep():
    """THE INVARIANT THAT KEEPS THE REPORT HONEST.

    The mode must be a VIEW of `paused`, not a second stored field. Two fields
    describing one fact drift, and the drift is invisible: the report would say
    ON DEMAND while the schedule kept sweeping every ten minutes, and the bill
    would be the first hint.
    """
    assert sweeptimer.mode_of(paused=True) == sweeptimer.MODE_ON_DEMAND
    assert sweeptimer.mode_of(paused=False) == sweeptimer.MODE_MONITOR


async def test_switching_mode_moves_the_flag_the_sweep_actually_reads(ctx):
    """Naming a mode has to change the thing that gates Slack calls.

    A mode that is only a label would be the worst outcome of this feature: the
    user believes they switched off automatic spending, and the passes keep
    being billed.
    """
    await hj.set_mode(ctx, AppModeParams(mode="on_demand"))
    state = await sweeptimer.describe(ctx)
    assert state["paused"] is True
    assert state["mode"] == sweeptimer.MODE_ON_DEMAND
    # And the gate agrees -- checked through due(), not just the stored value.
    ready, why = await sweeptimer.due(ctx)
    assert ready is False
    assert why == "paused"

    await hj.set_mode(ctx, AppModeParams(mode="monitor"))
    state = await sweeptimer.describe(ctx)
    assert state["paused"] is False
    assert state["mode"] == sweeptimer.MODE_MONITOR


async def test_on_demand_mode_keeps_the_interval_for_later(ctx):
    """Leaving monitor mode must not forget the interval.

    Otherwise coming back would resume at the default speed rather than the one
    the user chose -- a silent change in both behaviour and cost.
    """
    await hj.set_mode(ctx, AppModeParams(mode="monitor", minutes=180))
    await hj.set_mode(ctx, AppModeParams(mode="on_demand"))

    state = await sweeptimer.describe(ctx)
    assert state["interval_minutes"] == 180, "интервал потерян при выходе в режим по запросу"

    await hj.set_mode(ctx, AppModeParams(mode="monitor"))
    state = await sweeptimer.describe(ctx)
    assert state["interval_minutes"] == 180


async def test_an_unrecognised_mode_is_refused_rather_than_guessed(ctx):
    """One of the two modes spends money on a schedule.

    Guessing from an ambiguous word cannot be undone by switching back, because
    the passes are already billed -- so an unknown value must fail loudly.
    """
    result = await hj.set_mode(ctx, AppModeParams(mode="ерунда"))

    assert result.status == "error"
    assert "monitor" in result.error and "on_demand" in result.error
    # And nothing was changed on the way out.
    state = await sweeptimer.describe(ctx)
    assert state["configured"] is False, "отклонённый режим всё равно что-то записал"


def test_the_mode_field_has_no_default(ctx):
    """A vague instruction must not be able to start automatic spending.

    Same reasoning as the auto-reply switch: an omitted value has to be an
    error, not a silent vote for the expensive state.
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AppModeParams()


# --- the projection ----------------------------------------------------------

def test_the_projection_matches_the_interval_in_effect():
    """A month of passes at a given interval, arithmetically.

    Hard-coded expectations rather than a re-derivation of the same formula: a
    test that recomputes the implementation cannot catch the implementation
    being wrong.
    """
    assert sweeptimer.passes_per_month(5) == 8640      # 12/h * 24 * 30
    assert sweeptimer.passes_per_month(10) == 4320
    assert sweeptimer.passes_per_month(30) == 1440
    assert sweeptimer.passes_per_month(60) == 720
    assert sweeptimer.passes_per_month(1440) == 30     # once a day


def test_a_finer_interval_never_projects_fewer_passes():
    """Monotonic, across the whole offered range.

    The ladder in the report is only persuasive if it is ordered; a projection
    that dipped somewhere in the middle would make a faster setting look cheaper
    than a slower one.
    """
    ladder = sweeptimer.cost_ladder()
    passes = [row["passes"] for row in ladder]
    assert passes == sorted(passes, reverse=True), (
        f"лестница затрат не упорядочена: {passes}")


async def test_on_demand_mode_projects_nothing(ctx):
    """Zero, not "the interval it would use if it were running".

    The whole claim of on-demand mode is that it costs nothing automatically. A
    projection that still showed thousands of passes would contradict the mode
    it is describing.
    """
    await hj.set_mode(ctx, AppModeParams(mode="monitor", minutes=10))
    await hj.set_mode(ctx, AppModeParams(mode="on_demand"))

    state = await sweeptimer.describe(ctx)
    assert state["projected_passes"] == 0
    assert state["projected_tokens"] == 0


# --- actual spend ------------------------------------------------------------

async def test_only_real_passes_are_counted_not_ticks(connected_ctx, http):
    """THE ONE THAT PROTECTS THE BILL FROM THE ALARM CLOCK.

    A skipped tick costs nothing and must not be counted. If ticks were counted,
    every interval would bill the same ~8640 a month and the whole cost ladder
    would be fiction.
    """
    await hj.set_mode(connected_ctx, AppModeParams(mode="monitor", minutes=60))
    await sweeptimer.mark_ran(connected_ctx)

    before = (await sweeptimer.describe(connected_ctx))["billable_passes"]

    # Five ticks, none of them due.
    for _ in range(5):
        await hj.scheduled_catch_up(connected_ctx)

    after = (await sweeptimer.describe(connected_ctx))["billable_passes"]
    assert after == before, f"пропущенные тики посчитаны как платные: {before} -> {after}"


async def test_the_counter_survives_a_settings_change(ctx):
    """Changing the interval must not reset what was already spent.

    A counter that resets on every edit would let real spending hide behind a
    fresh-looking zero.
    """
    await sweeptimer.mark_ran(ctx)
    await sweeptimer.mark_ran(ctx)
    await hj.set_mode(ctx, AppModeParams(mode="monitor", minutes=45))

    state = await sweeptimer.describe(ctx)
    assert state["billable_passes"] == 2


async def test_spent_and_projected_are_reported_separately(ctx):
    """Two different questions, two different numbers.

    Collapsing them is how a report starts answering "what will this cost" with
    "what it has cost", which is the wrong number at the moment of choosing.
    """
    await hj.set_mode(ctx, AppModeParams(mode="monitor", minutes=60))
    await sweeptimer.mark_ran(ctx)

    state = await sweeptimer.describe(ctx)
    assert state["billable_passes"] == 1        # actual
    assert state["projected_passes"] == 720     # projected


# --- the change, spoken as a change ------------------------------------------

def test_a_cost_increase_is_stated_as_a_multiplier():
    """"в 6 раз" is the part that cannot be skimmed past.

    Two numbers side by side ask the reader to divide. The feature was requested
    precisely so the growth is obvious without arithmetic.
    """
    up = sweeptimer.compare({"projected_passes": 1440},
                            {"projected_passes": 8640})
    assert up["direction"] == "up"
    assert up["factor_text"] == "в 6 раз"


def test_a_cost_decrease_is_stated_too():
    """Hiding a saving would be the same dishonesty pointed the other way."""
    down = sweeptimer.compare({"projected_passes": 8640},
                              {"projected_passes": 720})
    assert down["direction"] == "down"
    assert down["factor_text"] == "в 12 раз"


def test_no_multiplier_is_invented_against_zero():
    """Leaving on-demand mode has no meaningful factor.

    0 -> 4320 is not "infinitely more expensive"; it is a different kind of
    change, and printing a made-up number would be worse than printing none.
    """
    out = sweeptimer.compare({"projected_passes": 0},
                             {"projected_passes": 4320})
    assert out["direction"] == "up"
    assert out["factor_text"] == "", "выдуман множитель против нуля"


async def test_speeding_up_the_monitor_says_it_costs_more(ctx):
    """End to end: the summary a person actually reads.

    Asserted on the text because the text is the deliverable -- a correct
    multiplier that never reaches the sentence changes nobody's mind.
    """
    await hj.set_mode(ctx, AppModeParams(mode="monitor", minutes=30))
    result = await hj.set_mode(ctx, AppModeParams(mode="monitor", minutes=5))

    assert result.status == "success", result.error
    assert "дороже" in result.summary, result.summary
    assert "в 6 раз" in result.summary, result.summary


async def test_leaving_monitor_mode_names_the_saving(ctx):
    """Switching off is a cost change as well, and worth stating."""
    await hj.set_mode(ctx, AppModeParams(mode="monitor", minutes=60))
    result = await hj.set_mode(ctx, AppModeParams(mode="on_demand"))

    assert result.status == "success", result.error
    assert "720" in result.summary, result.summary


# --- readable Russian --------------------------------------------------------

def test_counts_agree_with_their_nouns():
    """"144 проходов" is a machine fumbling its own grammar.

    These strings are read while deciding about money; sloppiness there
    undermines the figure being presented.
    """
    assert sweeptimer.passes_text(1) == "1 проход"
    assert sweeptimer.passes_text(2) == "2 прохода"
    assert sweeptimer.passes_text(5) == "5 проходов"
    # The exception everybody forgets.
    assert sweeptimer.passes_text(14) == "14 проходов"
    assert sweeptimer.passes_text(21) == "21 проход"
    assert sweeptimer.tokens_text(22) == "22 токена"


# --- the report --------------------------------------------------------------

async def test_the_status_shows_the_ladder_with_the_current_rung_marked(ctx):
    """One figure alone reads as a fact of life; the ladder makes it a choice."""
    await hj.set_mode(ctx, AppModeParams(mode="monitor", minutes=30))

    result = await hj.mode_status(ctx, AppModeStatusParams())

    assert result.status == "success", result.error
    text = result.summary
    # The cheap end and the expensive end both visible.
    assert "8640" in text and "30" in text
    assert "сейчас" in text, "текущий интервал не отмечен в лестнице"
