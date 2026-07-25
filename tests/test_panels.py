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
    assert "xoxb-test-token-one" not in _dump(
        await panels.slack_center(connected_ctx))
