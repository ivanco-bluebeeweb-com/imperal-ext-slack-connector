"""Per-function prices, and the two ways a price list quietly goes wrong.

WHY THIS IS TESTED AT ALL
-------------------------
Prices are data, not logic, so the instinct is to eyeball them once and move on.
Both failure modes here are silent and only surface as money:

  1. A NEW FUNCTION WITH NO PRICE. Twenty-nine functions today; the thirtieth
     arrives with a feature, nobody remembers the price table, and it is either
     billed at some platform default or not at all. Neither is a decision
     anybody made.

  2. THE MONITOR PRICE DRIFTING FROM THE PROJECTIONS. Every cost forecast this
     app shows -- "~4320 проходов = ~4320 токенов", the whole comparison ladder,
     "в 6 раз дороже" -- is computed from PRICE_PER_ACTION_TOKENS. If the price
     actually charged for a sweep pass stops matching that constant, the
     forecasts do not error. They just lie, and the user finds out from the bill.

The second one is the reason this file exists. It is the same class of bug as the
old SWEEP_CRON: two copies of one number, nothing comparing them.
"""

import json
import pathlib

import sweeptimer

MANIFEST = pathlib.Path(__file__).resolve().parent.parent / "imperal.json"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _pricing() -> dict:
    pricing = _manifest().get("pricing")
    assert pricing, "в манифесте нет блока pricing"
    return pricing


# --- every function has a price ----------------------------------------------

def test_every_function_has_a_price_and_no_price_is_orphaned():
    """THE INVARIANT THAT SURVIVES THE NEXT FEATURE.

    Checked in BOTH directions on purpose. A missing price means a function is
    billed by accident rather than by decision. An orphaned price means the
    table still mentions a function that no longer exists -- which is how a
    price list starts describing an app that has moved on.
    """
    tools = {t["name"] for t in _manifest()["tools"]}
    priced = set(_pricing()["tool_prices"])

    missing = tools - priced
    orphaned = priced - tools

    assert not missing, f"функции без цены: {sorted(missing)}"
    assert not orphaned, f"цены для несуществующих функций: {sorted(orphaned)}"


def test_no_price_is_negative_or_fractional():
    """Tokens are whole and non-negative. A negative price pays the user."""
    for name, price in _pricing()["tool_prices"].items():
        assert isinstance(price, int), f"{name}: цена не целая ({price!r})"
        assert price >= 0, f"{name}: отрицательная цена ({price})"


# --- the number the forecasts are built on -----------------------------------

def test_the_sweep_price_matches_the_constant_every_forecast_uses():
    """THE ONE THAT MATTERS MOST.

    `catch_up` is what a monitor pass actually calls, and every projection in
    this app -- the monthly figure, the comparison ladder, "в 6 раз дороже" --
    multiplies passes by PRICE_PER_ACTION_TOKENS. Two copies of one number with
    nothing comparing them is exactly the bug SWEEP_CRON was: it does not throw,
    it just makes every forecast wrong by a constant factor.
    """
    charged = _pricing()["tool_prices"]["catch_up"]

    assert charged == sweeptimer.PRICE_PER_ACTION_TOKENS, (
        f"цена прохода в манифесте ({charged}) расходится с константой "
        f"прогнозов ({sweeptimer.PRICE_PER_ACTION_TOKENS}) — все прогнозы "
        f"затрат врут в {charged or 1}/{sweeptimer.PRICE_PER_ACTION_TOKENS} раз")


def test_a_monitor_pass_is_never_free():
    """A free pass would make the whole cost feature pointless.

    If `catch_up` cost nothing, every projection would honestly read "0 токенов"
    and the two modes would be indistinguishable in price -- which is the exact
    question the user asked to be able to see.
    """
    assert _pricing()["tool_prices"]["catch_up"] > 0


# --- what free means ---------------------------------------------------------

def test_settings_and_status_functions_are_free():
    """Reading your own setting is not work, and must not be billed.

    Charging for `mode_status` would be charging someone to ask what they are
    being charged -- and it would discourage exactly the checking this feature
    was built to invite.
    """
    prices = _pricing()["tool_prices"]

    must_be_free = [
        "mode_status", "sweep_timer_status", "autoreply_status",
        "set_mode", "set_sweep_timer", "set_autoreply",
        "inbound_status", "check_access", "list_inbound", "list_workspaces",
    ]
    charged = {n: prices[n] for n in must_be_free if prices.get(n)}
    assert not charged, f"настройки и отчёты не должны стоить денег: {charged}"


def test_connecting_is_free_because_it_may_not_even_work():
    """Paying before the first success is paying for a failed attempt."""
    prices = _pricing()["tool_prices"]
    assert prices["connect_workspace"] == 0
    assert prices["connect_events"] == 0


def test_the_free_list_agrees_with_the_prices():
    """`free_tools` is a convenience view; it must not disagree with the table.

    A second list is a second truth. This is the same trap the mode/paused pair
    would have been, in a smaller place: kept only because it is derived, and
    asserted so it cannot drift.
    """
    pricing = _pricing()
    derived = sorted(n for n, v in pricing["tool_prices"].items() if v == 0)
    assert pricing["free_tools"] == derived


# --- writes that other people see --------------------------------------------

def test_writing_into_someone_elses_slack_costs_more_than_reading():
    """Posting is heavier than reading, and irreversible in social terms.

    Not a moral judgement -- a real cost difference: sending resolves the
    channel first, then writes something other humans get notified about.
    """
    prices = _pricing()["tool_prices"]
    assert prices["send_message"] > prices["read_channel"]
    assert prices["search_messages"] > prices["read_channel"]


def test_the_price_model_is_the_one_that_makes_growth_visible():
    """Per-action is the whole point: the interval has to show up in the bill.

    Under a flat subscription the monitor interval would be invisible to cost,
    which is the opposite of what was asked for.
    """
    pricing = _pricing()
    assert pricing["model"] == "per_action"
    assert pricing["currency"] == "tokens"
