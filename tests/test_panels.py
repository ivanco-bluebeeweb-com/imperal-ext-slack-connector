"""Panels: they must RENDER, and they must not fight over a slot.

Both bugs guarded here actually shipped in the Notion connector:

* two center panels claiming the same slot, which made the Connect button look
  dead because one silently replaced the other;
* a panel reading a key the data layer does not return, which raises INSIDE the
  panel -- where there is no error surface, so the user just sees an empty box.

Assertions walk the returned component tree rather than checking a type, because
"it returned something" is not the property that matters -- "it contains a form
that posts to the right action" is.
"""

import panels
from conftest import FAKE_BOT_TOKEN


def _flatten(node) -> list:
    """Every UI node in the tree, depth-first."""
    out: list = []
    if node is None:
        return out
    props = getattr(node, "props", None)
    if props is None:
        return out
    out.append(node)
    for value in props.values():
        if isinstance(value, list):
            for item in value:
                out.extend(_flatten(item))
        else:
            out.extend(_flatten(value))
    return out


def _types(node) -> list[str]:
    return [n.type for n in _flatten(node)]


def _dump(node) -> str:
    return " | ".join(
        f"{n.type}:{n.props}" for n in _flatten(node))


def _slot_of(spec) -> str:
    for attr in ("slot", "_slot"):
        if hasattr(spec, attr):
            return getattr(spec, attr)
    if isinstance(spec, dict):
        return spec.get("slot", "")
    return ""


# --- the slot-collision rule -------------------------------------------------

def test_only_one_panel_owns_each_slot():
    """THE bug that made the Notion Connect button look dead.

    A center slot holds exactly ONE panel with REPLACE semantics -- no stacking,
    no tabs. Two panels declaring slot="center" are both fetched at session init
    and one silently wins, so pressing a button that dispatches the loser does
    nothing visible while the shell re-renders around it.
    """
    from app import ext

    registered = getattr(ext, "_panels", {}) or {}
    slots: dict[str, list[str]] = {}
    for name, spec in registered.items():
        slot = _slot_of(spec)
        if not slot:
            continue
        # "secrets" is injected by the platform, not this app.
        if name == "secrets":
            continue
        slots.setdefault(slot, []).append(name)

    collisions = {s: n for s, n in slots.items() if len(n) > 1}
    assert not collisions, f"more than one panel per slot: {collisions}"


def test_there_is_no_separate_connect_panel():
    """One owner for center; the view is a parameter."""
    from app import ext

    registered = getattr(ext, "_panels", {}) or {}
    assert "slack" in registered
    assert "connect" not in registered


# --- rendering ---------------------------------------------------------------

async def test_center_panel_renders_a_connect_invitation_with_no_token(ctx):
    """First-run state: it must invite connecting, not look broken."""
    tree = await panels.slack_center(ctx)
    assert _types(tree), "panel rendered nothing"
    assert "connect" in _dump(tree).lower()


async def test_the_connect_view_posts_to_this_extensions_own_function(ctx):
    """save_app_secret belongs to the developer extension, not to this one.

    A panel action resolves against the functions of the RENDERING extension,
    so ui.Form(action="save_app_secret") fails at click time with "Function
    'save_app_secret' not found". The documented recipe only works from inside
    the extension that owns the action.
    """
    tree = await panels.slack_center(ctx, view="connect")
    blob = _dump(tree)
    assert "connect_workspace" in blob
    assert "save_app_secret" not in blob


async def test_the_connect_view_has_a_masked_field_for_the_token(ctx):
    """A token is a credential: it must not render as plain text.

    ui.Password is not its own node type -- it renders as an Input carrying
    props["type"] == "password", so that is what has to be asserted.
    """
    tree = await panels.slack_center(ctx, view="connect")
    assert "Form" in _types(tree)
    masked = [n for n in _flatten(tree)
              if n.type == "Input" and n.props.get("type") == "password"]
    assert masked, "the token field must be masked"


async def test_center_panel_renders_with_a_configured_token(connected_ctx, http):
    from conftest import auth_test_payload
    http.push(auth_test_payload(team="Acme"))
    tree = await panels.slack_center(connected_ctx)
    assert "Acme" in _dump(tree)


async def test_a_rejected_token_renders_as_a_diagnosis_not_an_exception(
        connected_ctx, http):
    from conftest import err
    http.push(err("invalid_auth"))
    tree = await panels.slack_center(connected_ctx)
    types = _types(tree)
    assert types, "panel rendered nothing for a dead token"
    # It must SAY something is wrong rather than showing a blank workspace row.
    assert "Alert" in types or "Empty" in types or "Badge" in types


async def test_nav_panel_renders_with_no_token(ctx):
    assert _types(await panels.slack_nav(ctx))


async def test_nav_panel_renders_with_a_token(connected_ctx, http):
    from conftest import auth_test_payload
    http.push(auth_test_payload(team="Acme"))
    assert _types(await panels.slack_nav(connected_ctx))


async def test_panels_never_raise_when_the_store_is_unreadable(ctx):
    """A panel has no error surface -- an exception there shows an empty box."""
    class Boom:
        async def get(self, *a, **k):
            raise RuntimeError("store down")

        async def set(self, *a, **k):
            raise RuntimeError("store down")

    ctx.secrets = Boom()
    assert _types(await panels.slack_center(ctx))
    assert _types(await panels.slack_nav(ctx))


async def test_no_panel_leaks_a_token_into_its_markup(connected_ctx, http):
    from conftest import auth_test_payload
    http.push(auth_test_payload(team="Acme"))
    assert FAKE_BOT_TOKEN not in _dump(
        await panels.slack_center(connected_ctx))


# --- the inbound events view -------------------------------------------------
# Its own block because this view's job is to carry FACTS the user must copy
# into Slack. A view that renders but shows the wrong URL, or omits a scope, is
# indistinguishable from a working one on screen -- and produces an endpoint
# that is silently never called.

SIGNING_SECRET_VALUE = "abcdef0123456789abcdef0123456789"


async def test_the_events_view_shows_the_endpoint_url_to_paste(connected_ctx,
                                                               http):
    from conftest import auth_test_payload
    http.push(auth_test_payload(team="Acme"))
    dump = _dump(await panels.slack_center(connected_ctx, view="events"))
    # The path is what Slack must call. Built by the SDK from the kernel app id,
    # never hardcoded here -- so this asserts the SHAPE, not a literal host.
    assert "/webhook/events" in dump


async def test_the_events_view_form_posts_to_this_extensions_own_function(
        connected_ctx, http):
    """A panel action resolves against THIS extension's functions.

    Pointing the form at `save_app_secret` (the developer extension) is the
    documented trap: it fails at click time with "Function not found".
    """
    from conftest import auth_test_payload
    http.push(auth_test_payload(team="Acme"))
    dump = _dump(await panels.slack_center(connected_ctx, view="events"))
    assert "connect_events" in dump
    assert "save_app_secret" not in dump


async def test_the_events_view_lists_every_scope_inbound_needs(connected_ctx,
                                                              http):
    """A missing history scope delivers NOTHING, with no error anywhere."""
    from conftest import auth_test_payload
    http.push(auth_test_payload(team="Acme"))
    dump = _dump(await panels.slack_center(connected_ctx, view="events"))
    for scope in ("app_mentions:read", "channels:history", "groups:history",
                  "im:history", "users:read"):
        assert scope in dump, f"{scope} not shown to the user"


async def test_the_events_view_lists_the_slack_subscriptions_to_tick(
        connected_ctx, http):
    from conftest import auth_test_payload
    http.push(auth_test_payload(team="Acme"))
    dump = _dump(await panels.slack_center(connected_ctx, view="events"))
    for subscription in ("app_mention", "message.channels", "message.groups",
                         "message.im"):
        assert subscription in dump


async def test_the_events_view_names_the_imperal_events_for_rule_building(
        connected_ctx, http):
    from conftest import auth_test_payload
    http.push(auth_test_payload(team="Acme"))
    dump = _dump(await panels.slack_center(connected_ctx, view="events"))
    for event in ("slack-connector.message_received",
                  "slack-connector.app_mentioned",
                  "slack-connector.thread_reply_received",
                  "slack-connector.dm_received"):
        assert event in dump


async def test_the_events_view_warns_when_the_signing_secret_is_missing(
        connected_ctx, http):
    from conftest import auth_test_payload
    http.push(auth_test_payload(team="Acme"))
    dump = _dump(await panels.slack_center(connected_ctx, view="events"))
    assert "warning" in dump.lower()


async def test_the_events_view_never_shows_the_signing_secret_back(ctx, http):
    """Set/not-set is the ONLY thing the panel may reveal about a secret."""
    from imperal_sdk.testing import MockSecretStore
    from conftest import auth_test_payload

    ctx.secrets = MockSecretStore({
        "slack_tokens": FAKE_BOT_TOKEN,
        "slack_signing_secret": SIGNING_SECRET_VALUE,
    })
    http.push(auth_test_payload(team="Acme"))
    dump = _dump(await panels.slack_center(ctx, view="events"))
    assert SIGNING_SECRET_VALUE not in dump
    assert FAKE_BOT_TOKEN not in dump


# --- inbound state must be visible WITHOUT hunting for it --------------------
# The events view is excellent once you reach it. The bug this block guards is
# that you had no reason to reach it: a workspace connects fine, sending works,
# and nothing anywhere says inbound is dead. That is the silent half-configured
# endpoint the events view docstring itself warns about -- so the warning has to
# live on the screen people actually land on.

async def test_workspaces_view_warns_when_inbound_is_not_configured(
        connected_ctx, http):
    """Connected + no signing secret = Webbee cannot see a single message."""
    from conftest import auth_test_payload
    http.push(auth_test_payload(team="Acme"))
    tree = await panels.slack_center(connected_ctx, view="workspaces")
    # A BUTTON labelled "Set up incoming events" already existed and is not
    # enough: it reads as an optional extra, not as "this is switched off".
    # The property under test is that an ALERT states the current state.
    alerts = [n for n in _flatten(tree) if n.type == "Alert"]
    said = " ".join(str(a.props.get("message", "")) for a in alerts).lower()
    assert "not receiving" in said, (
        "the default screen never warns that inbound is unconfigured, so a "
        "user has no idea events are dead")


async def test_workspaces_view_is_quiet_once_inbound_is_configured(ctx, http):
    """No nagging when it is actually set up -- a banner that never clears
    trains people to ignore banners."""
    from imperal_sdk.testing import MockSecretStore
    from conftest import auth_test_payload

    ctx.secrets = MockSecretStore({
        "slack_tokens": FAKE_BOT_TOKEN,
        "slack_signing_secret": SIGNING_SECRET_VALUE,
    })
    http.push(auth_test_payload(team="Acme"))
    dump = _dump(await panels.slack_center(ctx, view="workspaces"))
    assert "not receiving" not in dump.lower()


async def test_nav_shows_inbound_is_off(connected_ctx, http):
    """The sidebar is the always-visible surface; state belongs there."""
    from conftest import auth_test_payload
    http.push(auth_test_payload(team="Acme"))
    dump = _dump(await panels.slack_nav(connected_ctx))
    assert "event" in dump.lower()


async def test_workspaces_view_survives_an_unreadable_secret_store(
        connected_ctx, http):
    """Reading the secret must never turn the main screen into an empty box."""
    from conftest import auth_test_payload

    class Boom:
        async def get(self, *a, **k):
            raise RuntimeError("store down")

        async def set(self, *a, **k):
            raise RuntimeError("store down")

    http.push(auth_test_payload(team="Acme"))
    records = await panels.acc.list_workspaces(connected_ctx)
    connected_ctx.secrets = Boom()
    assert _types(await panels.slack_center(connected_ctx, view="workspaces"))
    assert records is not None
