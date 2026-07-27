"""Static contract sweep over the whole app.

The WP Publisher incident is the reason this file exists: errors were emitted
without a structured `code`, the kernel stamped EXT_UNSTRUCTURED_ERROR, and no
validator rule caught it because the app routed through a local helper instead
of calling ActionResult.error directly. Validator rule V32 matches the literal
call, so it stayed silent.

A test that walks the AST does not care which helper is used -- it checks every
error path in the SOURCE, so the same class of bug cannot come back quietly as
tools are added later.

Two of the sweeps below are Slack-specific:

* `ts` must never be coerced to a number. It is a message's IDENTITY, and a
  float loses microsecond precision -- the resulting ts is one Slack no longer
  recognises, so replies land in the wrong place or vanish.
* every write must declare `event=`, which is what lets automations trigger on
  it. A write without one is invisible to the rest of the OS.
"""

import ast
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parent.parent

# DISCOVERED, not listed. A hand-maintained list silently stops covering the
# code the moment a handler file is added or renamed -- which is exactly when a
# sweep like this matters most. Globbing means a new handler is covered the day
# it lands.
HANDLER_FILES = sorted(
    p.name for p in APP_DIR.glob("handlers_*.py")
) + ["shared.py", "accounts.py", "slack_client.py", "panels.py"]
WRITE_FILES = sorted(p.name for p in APP_DIR.glob("handlers_*.py"))
ALL_FILES = HANDLER_FILES + ["app.py", "models.py", "slack_objects.py",
                             "main.py"]


def _tree(name: str) -> ast.AST:
    return ast.parse((APP_DIR / name).read_text())


def _calls(tree: ast.AST, *names: str):
    """Every Call node whose callee is one of `names` (attribute or plain)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        label = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if label in names:
            yield node


# --- structured errors -------------------------------------------------------

def test_every_error_result_carries_a_structured_code():
    """No ActionResult.error() anywhere without an explicit code=."""
    offenders = []
    for name in HANDLER_FILES:
        for call in _calls(_tree(name), "error"):
            fn = call.func
            # Only ActionResult.error(...) -- not ctx.log.error(...)
            if not (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "ActionResult"):
                continue
            if not any(kw.arg == "code" for kw in call.keywords):
                offenders.append(f"{name}:{call.lineno}")
    assert not offenders, f"ActionResult.error without code=: {offenders}"


def test_the_local_error_helper_always_requires_a_code():
    """shared.error(message, code) -- code is positional and mandatory.

    A default would let a call site silently omit it, which is exactly how the
    unstructured-error bug happened before.
    """
    tree = _tree("shared.py")
    helper = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "error")
    args = [a.arg for a in helper.args.args]
    assert args[:2] == ["message", "code"]
    # defaults align to the TAIL of the arg list; `code` must not have one.
    assert len(helper.args.defaults) <= len(args) - 2


def test_every_error_helper_call_passes_a_code():
    """Each _error(...) call site supplies the code argument."""
    offenders = []
    for name in WRITE_FILES:
        for call in _calls(_tree(name), "_error"):
            has_positional_code = len(call.args) >= 2
            has_kw_code = any(kw.arg == "code" for kw in call.keywords)
            if not (has_positional_code or has_kw_code):
                offenders.append(f"{name}:{call.lineno}")
    assert not offenders, f"_error() without a code: {offenders}"


def test_every_client_fail_call_passes_a_code():
    """sc.fail(code, ...) -- the client's envelope builder needs a code too."""
    offenders = []
    for name in HANDLER_FILES:
        for call in _calls(_tree(name), "fail"):
            if not call.args and not any(kw.arg == "code" for kw in call.keywords):
                offenders.append(f"{name}:{call.lineno}")
    assert not offenders, f"fail() without a code: {offenders}"


# --- nothing leaks to the user ----------------------------------------------

def test_no_user_facing_message_interpolates_an_exception():
    """Raw exception text must not reach the user; it goes to the audit log.

    Catches f-strings containing {e}/{exc}/{err} inside an ActionResult.error
    or _error call -- the shape that leaks tracebacks into chat.
    """
    offenders = []
    leaky = {"e", "exc", "err", "error"}
    for name in HANDLER_FILES:
        for call in _calls(_tree(name), "error", "_error"):
            fn = call.func
            is_action_error = (isinstance(fn, ast.Attribute)
                               and isinstance(fn.value, ast.Name)
                               and fn.value.id == "ActionResult")
            is_helper = getattr(fn, "id", "") == "_error"
            if not (is_action_error or is_helper):
                continue
            for arg in call.args:
                if not isinstance(arg, ast.JoinedStr):
                    continue
                for piece in ast.walk(arg):
                    if (isinstance(piece, ast.FormattedValue)
                            and isinstance(piece.value, ast.Name)
                            and piece.value.id in leaky):
                        offenders.append(f"{name}:{call.lineno}")
    assert not offenders, f"exception text in user-facing message: {offenders}"


def test_no_token_is_ever_written_to_the_store():
    """Only the Vault secret holds tokens; the store keeps metadata only.

    A token in the store is a credential in a place that is backed up, exported
    and rendered into panels -- none of which is true of the Vault.
    """
    for name in ["accounts.py", "panels.py"] + WRITE_FILES:
        tree = _tree(name)
        for call in _calls(tree, "insert", "update", "set", "put"):
            for arg in list(call.args) + [kw.value for kw in call.keywords]:
                for node in ast.walk(arg):
                    if isinstance(node, ast.Name) and node.id == "token":
                        raise AssertionError(
                            f"token passed to a store call at {name}:{call.lineno}")


def test_no_print_statements_survive_in_shipped_code():
    for name in ALL_FILES:
        for call in _calls(_tree(name), "print"):
            raise AssertionError(f"print() left in {name}:{call.lineno}")


# --- Slack-specific invariants ----------------------------------------------

def test_a_timestamp_is_never_coerced_to_a_number():
    """`ts` is IDENTITY, not a quantity.

    float("1690000000.000100") -> 1690000000.0001, whose string form is a ts
    Slack does not recognise. So no float()/int() may ever wrap a ts anywhere
    outside slack_objects.humanize_ts, which parses a COPY purely to format a
    date and never feeds the result back to Slack.
    """
    offenders = []
    for name in HANDLER_FILES + ["slack_objects.py"]:
        tree = _tree(name)
        for call in _calls(tree, "float", "int"):
            for arg in call.args:
                for node in ast.walk(arg):
                    is_ts_name = (isinstance(node, ast.Name)
                                  and node.id in {"ts", "thread_ts"})
                    is_ts_key = (isinstance(node, ast.Constant)
                                 and node.value in {"ts", "thread_ts"})
                    if is_ts_name or is_ts_key:
                        offenders.append(f"{name}:{call.lineno}")
    # humanize_ts is the ONE sanctioned parse: it formats a date and returns a
    # string that never goes back to Slack.
    allowed_line = next(
        n.lineno
        for n in ast.walk(_tree("slack_objects.py"))
        if isinstance(n, ast.FunctionDef) and n.name == "humanize_ts")
    offenders = [o for o in offenders
                 if not (o.startswith("slack_objects.py")
                         and abs(int(o.split(":")[1]) - allowed_line) < 25)]
    assert not offenders, f"ts coerced to a number at: {offenders}"


def test_every_write_declares_an_event():
    """A write without event= is invisible to automations.

    The whole point of the OS is that one app's write can trigger another's
    work; a tool that mutates Slack silently cannot participate in that.
    """
    missing = []
    for name in WRITE_FILES:
        for node in ast.walk(_tree(name)):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                kinds = {kw.arg: kw.value for kw in dec.keywords}
                action = kinds.get("action_type")
                if not (isinstance(action, ast.Constant)
                        and action.value in ("write", "destructive")):
                    continue
                if "event" not in kinds:
                    missing.append(f"{name}:{node.name}")
    assert not missing, f"write tools without event=: {missing}"


def test_deleting_a_message_is_classified_destructive():
    """Slack has no undo for a deleted message.

    action_type="destructive" is what makes the kernel's two-step confirmation
    guard intercept the call, so the gate is declared rather than hand-rolled.
    """
    found = False
    for name in WRITE_FILES:
        for node in ast.walk(_tree(name)):
            if not (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "delete_message"):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                for kw in dec.keywords:
                    if kw.arg == "action_type":
                        assert isinstance(kw.value, ast.Constant)
                        assert kw.value.value == "destructive", (
                            "delete_message must be destructive, not "
                            f"{kw.value.value!r}")
                        found = True
    assert found, "delete_message has no action_type at all"


def test_every_tool_that_reaches_slack_goes_through_the_client():
    """No handler may call ctx.http directly.

    The client is where the `ok: false` handling, the timeout and the error
    classification live. A direct ctx.http call bypasses all three, and the
    first symptom would be a Slack failure reported to the user as a success.
    """
    offenders = []
    for name in WRITE_FILES + ["panels.py"]:
        tree = _tree(name)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "http"
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id == "ctx"):
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, f"direct ctx.http use outside the client: {offenders}"


# --- UI component keyword arguments -----------------------------------------

def test_every_ui_component_call_uses_only_real_keyword_arguments():
    """Panels must only pass keywords the ui.* components actually accept.

    The platform validator rejects a deploy for this, but the LOCAL validator
    does not check it -- so without this test the failure is only discovered
    after a push, one wrong keyword per round trip. Two real examples caught
    here: ui.Empty has no `action_label`, and ui.Button takes `on_click` rather
    than `action`.

    Checking against the live signatures means the test cannot drift from the
    SDK: it reads whatever the installed version actually accepts.
    """
    import inspect
    from imperal_sdk import ui

    problems = []
    for name in ("panels.py",):
        for node in ast.walk(_tree(name)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "ui"):
                continue
            component = getattr(ui, fn.attr, None)
            if component is None:
                problems.append(f"{name}:{node.lineno} ui.{fn.attr} does not exist")
                continue
            try:
                sig = inspect.signature(component)
            except (TypeError, ValueError):
                continue
            if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                continue
            valid = set(sig.parameters)
            for kw in node.keywords:
                if kw.arg and kw.arg not in valid:
                    problems.append(
                        f"{name}:{node.lineno} ui.{fn.attr}({kw.arg}=...) "
                        f"invalid; accepts {sorted(valid)}")
    assert not problems, "invalid ui.* keyword arguments: " + "; ".join(problems)


# --- an entity must be readable in chat, not just in the panel ---------------

def _message_entities():
    """Entity classes that represent a message someone actually wrote."""
    import models as m

    return [m.InboundMessage, m.MessageRecord, m.SearchHit]


def test_a_message_entity_is_titled_by_its_text():
    """The title of a message is WHAT WAS SAID.

    Found live: the journal titled rows by author, so a message arrived in chat
    as "Vladislav Ivanco · Vladislav Ivanco · inboundmessage" -- the words
    nowhere in sight. The panel rendered the full record and showed the text,
    so the same message was visible on one surface and unreadable on the other.
    Webbee cannot mention what she cannot read: the message was "received" and
    still effectively missing.

    Asserted over EVERY message entity, because the mistake was copy-pasted
    across four of them and fixing only the one that got reported would leave
    the others waiting to be discovered the same way.
    """
    for cls in _message_entities():
        assert cls._title_field == "text", (
            f"{cls.__name__} is titled by '{cls._title_field}', not by the "
            f"message text — it will arrive in chat without the words")


def test_a_message_entity_says_who_and_where_in_its_subtitle():
    """Author and location belong in the subtitle, not the title."""
    for cls in _message_entities():
        parts = cls._subtitle_parts
        assert parts, f"{cls.__name__} has no subtitle: chat shows no context"
        assert "author" in parts, f"{cls.__name__} subtitle omits the author"


def test_a_message_id_is_not_mistaken_for_a_phone_number():
    """A bare Slack ts is a long digit string and gets redacted as PII.

    Found live: ids arrived in chat as "<PHONE>" — the platform's PII guard
    read "1785176897.496389" as a phone number. The id was then useless for
    referring back to the message.

    The prefix must keep the timestamp RECOVERABLE: `ts` is a message's
    identity, and an id you cannot map back to it cannot address a reply.
    """
    import models as m

    ts = "1785176897.496389"
    msg = m.InboundMessage(text="привет", author="Vlad", ts=ts)

    assert msg.id != ts, "a bare ts id is redacted as a phone number"
    assert not str(msg.id).replace(".", "").isdigit(), (
        f"id {msg.id!r} is still all digits and will be redacted")
    assert str(msg.id).split(":", 1)[1] == ts, (
        "the timestamp must stay recoverable from the id, or replies to this "
        "message can no longer be addressed")


def test_a_message_with_no_text_still_has_a_name():
    """An attachment-only message must not render as a nameless row."""
    import models as m

    with_file = m.InboundMessage(text="", author="Vlad", ts="1.1",
                                 has_files=True)
    without = m.InboundMessage(text="", author="Vlad", ts="1.2")

    assert with_file.title.strip(), "attachment-only message has no title"
    assert "влож" in with_file.title.lower(), (
        f"title {with_file.title!r} does not say there is an attachment")
    assert without.title.strip(), "empty message renders as a nameless row"


def test_a_direct_message_says_so_instead_of_showing_a_blank_channel():
    """A DM has no channel name; the subtitle must not show an empty gap."""
    import models as m

    dm = m.InboundMessage(text="привет", author="Vlad", is_dm=True,
                          ts="1.3", posted_at="вчера")

    assert dm.subtitle, "a DM has no subtitle at all"
    assert "·  ·" not in dm.subtitle, (
        f"subtitle {dm.subtitle!r} has an empty slot where the channel was")
    assert "личное" in dm.subtitle.lower(), (
        f"subtitle {dm.subtitle!r} does not say this is a direct message")
