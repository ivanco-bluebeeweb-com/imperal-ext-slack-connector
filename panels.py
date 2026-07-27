"""Panels: connect first, then show what is reachable and why.

Two surfaces:

* ``slack``     -- ONE center panel with two views (``connect`` / ``workspaces``)
* ``slack_nav`` -- left sidebar: connection state at a glance

WHY ONE CENTER PANEL AND NOT TWO
The Notion connector shipped `connect` and `workspaces` as two separate panels,
both slot="center" with center_overlay=True. The host fetches every configured
slot in ONE batch at session init, and a center slot holds exactly ONE panel
with REPLACE semantics -- no stacking, no tabs. Two overlay panels claiming the
same slot therefore race: both are fetched, one silently replaces the other,
and pressing a button that dispatches the loser looks like nothing happening
while the shell re-renders around it. The reported symptom was "the left
sidebar reloads and nothing happens", and no amount of fixing the button could
have cured it. Here there is one owner from the start, and the view is a
parameter:

    ui.Call("__panel__slack")                      -> workspaces (default)
    ui.Call("__panel__slack", view="connect")      -> connect screen
    ui.Call("__panel__slack", refresh=True)        -> workspaces, re-read

CREDENTIAL HANDLING (federal EXT-SECRETS-V1)
``slack_tokens`` is declared write_mode="both", so the app itself may store the
token -- which is what lets the Connect screen own the whole flow instead of
sending the user off to the Secrets tab and hoping. The form posts to
``connect_workspace``, a function of THIS extension.

Do NOT use ``ui.Form(action="save_app_secret")``: a panel action resolves
against the functions of the RENDERING extension, and save_app_secret belongs
to the developer extension, so it fails at click time with "Function
'save_app_secret' not found". That documented recipe only works from inside the
extension that owns the action.

PROP NAMES ARE NOT INTERCHANGEABLE
``ui.Text`` takes ``content=``; ``ui.Header`` takes ``text=``. Mixing them up
passes the local validator and is rejected by the platform at deploy time.
"""

from __future__ import annotations

from imperal_sdk import ui

import accounts as acc
import journal
from app import ext

# The platform's own secrets manager, for anyone who prefers to paste there (or
# needs to add a SECOND workspace token on its own line).
_SECRETS_ROUTE = f"/ext/{ext.app_id}/secrets#{acc.SECRET_NAME}"
_SLACK_APPS_URL = "https://api.slack.com/apps"
_SIGNING_SECRET_NAME = "slack_signing_secret"

# Recommended scopes. Stated explicitly because Slack's app creation screen
# offers dozens and picking wrong means reinstalling later -- and because a
# missing scope surfaces as an error the user cannot otherwise interpret.
_BOT_SCOPES = ("channels:read, groups:read, im:read, mpim:read, "
               "channels:history, groups:history, users:read, chat:write, "
               "reactions:write, pins:write")

# Scopes that INBOUND specifically needs, listed separately because they are the
# ones people miss: an event subscription with no matching scope is accepted by
# Slack's UI and then delivers nothing at all, with no error anywhere.
_HISTORY_SCOPES = (
    "app_mentions:read   — receive @-mentions of the app\n"
    "channels:history    — messages in PUBLIC channels\n"
    "groups:history      — messages in PRIVATE channels\n"
    "im:history          — direct messages to the app\n"
    "users:read          — turn a user id into a display name"
)


async def _signing_secret_is_set(ctx) -> bool:
    """Whether the signing secret exists -- as a BOOLEAN, never the value.

    One helper because three surfaces now need this fact (events view, default
    view, sidebar), and each one reading the secret itself is three chances to
    leak it into markup or to crash a panel on an unreadable store. A panel has
    no error surface: an exception here renders an empty box, so an unreadable
    store must degrade to "not set" rather than propagate.
    """
    try:
        return bool(await ctx.secrets.get(_SIGNING_SECRET_NAME))
    except Exception:
        return False


def _connect_view(records: list[dict]) -> ui.Component:
    """The screen a first-time user lands on: paste a token, in three steps.

    SKETCH -- connect screen (props checked against ui-components-reference)
      ui.Stack (v, gap=4)
        ui.Header(text="Connect Slack", level=2, subtitle=...)
        ui.Alert(...)                       -- already-connected notice, if any
        ui.Section(title="1. Create a Slack app", children=[
          ui.Text(content=..., variant="body")     -- content=, NOT text=
          ui.Link(label="Open api.slack.com/apps", href=...)
        ])
        ui.Section(title="2. Paste the token", children=[
          ui.Text(content=...)
          ui.Form(action="connect_workspace", submit_label="Connect", children=[
            ui.Password(placeholder="xoxb-...", param_name="token")
          ])
          ui.Link(label="Or manage the stored tokens directly", href=_SECRETS_ROUTE)
        ])
        ui.Section(title="3. Invite the app", children=[
          ui.Text(content=...)
          ui.Button(label="Check what is reachable", ...)
        ])
    """
    children: list = [
        ui.Header(text="Connect Slack", level=2,
                  subtitle="Three steps, about a minute."),
    ]

    if records:
        names = ", ".join(
            r.get("workspace_name") or "a workspace" for r in records)
        children.append(ui.Alert(
            message=(f"Already connected: {names}. Pasting another token ADDS a "
                     "workspace -- it never replaces the ones already here."),
            type="info"))

    children.append(ui.Section(title="1. Create a Slack app", children=[
        ui.Text(content=(
            "Open api.slack.com/apps and create an app in the workspace you "
            "want to reach. Under OAuth & Permissions add the bot scopes you "
            f"need -- a good default set is: {_BOT_SCOPES} -- then click "
            "Install to Workspace and copy the Bot User OAuth Token."),
            variant="body"),
        ui.Text(content=(
            "A bot token (xoxb-) is the right choice: it keeps working after "
            "the person who installed it leaves. A user token (xoxp-) is also "
            "accepted, and it is the ONLY way to search messages -- Slack does "
            "not offer search to bots."),
            variant="caption"),
        ui.Link(label="Open api.slack.com/apps", href=_SLACK_APPS_URL),
    ]))

    children.append(ui.Section(title="2. Paste the token", children=[
        ui.Text(content=(
            "The token is checked against Slack before it is saved, so you find "
            "out immediately whether it works. It is stored encrypted and never "
            "shown back -- not even to you."),
            variant="body"),
        ui.Form(
            action="connect_workspace",
            submit_label="Connect",
            children=[ui.Password(placeholder="xoxb-...", param_name="token")],
        ),
        ui.Link(label="Or manage the stored tokens directly", href=_SECRETS_ROUTE),
    ]))

    children.append(ui.Section(title="3. Invite the app to your channels", children=[
        ui.Text(content=(
            "A fresh Slack app is in no channel yet, so reading history or "
            "posting will fail until you invite it. In Slack, open the channel "
            "and type /invite @your-app. Public channels can be LISTED without "
            "this, but not read."),
            variant="body"),
        ui.Button(label="Check what is reachable",
                  on_click=ui.Call("__panel__slack", view="workspaces",
                                   refresh=True)),
    ]))

    return ui.Stack(direction="vertical", gap=4, children=children)


def _events_view(records: list[dict], secret_set: bool,
                 endpoint_url: str, pushed_count: int = 0) -> ui.Component:
    """Set up INBOUND events: the endpoint URL, the secret, the subscriptions.

    Its own view because inbound setup happens in the SLACK console, not here,
    and the user needs three things in front of them at once: the URL to paste,
    the secret to copy back, and the exact list of events to tick. Splitting
    those across screens is how a half-configured endpoint happens -- and a
    half-configured endpoint fails silently, which is the worst outcome to
    debug.

    SKETCH -- events view
      ui.Stack (v, gap=4)
        ui.Header(text="Incoming Slack events", level=2, subtitle=...)
        ui.Alert(...)                          -- ready / not ready
        ui.Section("1. Request URL")     [ui.Text, ui.Code, ui.Text]
        ui.Section("2. Signing secret")  [ui.Text, ui.Form(Password), ui.Link]
        ui.Section("3. Subscribe")       [ui.Text, ui.Code, ui.Text]
        ui.Section("4. Scopes")          [ui.Text, ui.Code, ui.Text]
        ui.Section("What Webbee then sees") [ui.Text, ui.Code]
    """
    usable = [r for r in records if r.get("status") == "ok"]
    ready = bool(secret_set and usable)

    children: list = [
        ui.Header(text="Incoming Slack events", level=2,
                  subtitle="Let Webbee see messages, mentions and thread "
                           "replies — and answer in the right place."),
    ]

    if ready and pushed_count > 0:
        children.append(ui.Alert(
            message=(f"Inbound is working — {pushed_count} message(s) have "
                     "arrived this way. Nothing left to do here."),
            type="success"))
    elif ready:
        # A saved secret is NOT proof of delivery. Announcing "configured, the
        # event reaches Imperal" while zero messages have ever arrived is the
        # silent failure this screen exists to prevent: steps 1, 3 and 4 happen
        # in the SLACK console, and a secret saved here cannot tell whether
        # anybody did them.
        children.append(ui.Alert(
            message=("Signing secret saved — that was step 2 of 4. Slack sends "
                     "nothing until the rest is done in the Slack console: "
                     "paste the Request URL (step 1), subscribe to the four "
                     "bot events (step 3), then Reinstall the app (step 4). "
                     "No message has arrived by push yet."),
            type="warning"))
    elif not usable:
        children.append(ui.Alert(
            message=("Connect a workspace token first — an inbound event still "
                     "needs a token to look up who wrote it and to reply."),
            type="warning"))
    else:
        children.append(ui.Alert(
            message=("The signing secret is not set yet, so every incoming "
                     "delivery is refused. Step 2 below fixes that."),
            type="warning"))

    children.append(ui.Section(title="1. Paste this Request URL into Slack",
                               children=[
        ui.Text(content=(
            "In Slack → your app → Event Subscriptions, switch Enable Events "
            "on and paste this as the Request URL."),
            variant="body"),
        ui.Code(content=endpoint_url, language="text"),
        ui.Text(content=(
            "Slack immediately calls it once to verify ownership. That "
            "challenge is answered automatically — you should see 'Verified' "
            "straight away. If you do not, the app is not deployed yet."),
            variant="caption"),
    ]))

    children.append(ui.Section(title="2. Paste the signing secret back here",
                               children=[
        ui.Text(content=(
            "Slack signs every delivery with this secret, and the connector "
            "refuses anything that is not correctly signed — otherwise anyone "
            "who learned the URL above could fake messages from your team."),
            variant="body"),
        ui.Text(content=(
            "Find it in Slack → your app → Basic Information → App "
            "Credentials → Signing Secret (press Show). It is NOT the xoxb- "
            "token."),
            variant="caption"),
        ui.Form(
            action="connect_events",
            submit_label="Save signing secret" if not secret_set
                         else "Replace signing secret",
            children=[ui.Password(placeholder="32-character secret",
                                  param_name="signing_secret")],
        ),
    ]))

    children.append(ui.Section(title="3. Subscribe to these bot events",
                               children=[
        ui.Text(content=(
            "Still on Event Subscriptions, open 'Subscribe to bot events' and "
            "add these four. Each one is a different way a person can talk to "
            "the app:"),
            variant="body"),
        ui.Code(content=("app_mention        — someone @-mentions the app\n"
                         "message.channels   — public channel messages\n"
                         "message.groups     — private channel messages\n"
                         "message.im         — direct messages to the app"),
                language="text"),
        ui.Text(content=(
            "Thread replies arrive through these same subscriptions; Slack has "
            "no separate 'reply' event. The connector tells a reply from a new "
            "message itself."),
            variant="caption"),
    ]))

    children.append(ui.Section(title="4. Scopes — and why a reinstall may be "
                                     "needed", children=[
        ui.Text(content=(
            "Subscribing to an event Slack has no scope for silently delivers "
            "nothing. These are the history scopes inbound needs:"),
            variant="body"),
        ui.Code(content=_HISTORY_SCOPES, language="text"),
        ui.Text(content=(
            "Adding a scope in OAuth & Permissions requires reinstalling the "
            "app to the workspace, and reinstalling issues a NEW token — paste "
            "it on the Connect screen afterwards, or the connector keeps using "
            "the old one."),
            variant="caption"),
    ]))

    children.append(ui.Section(title="What Webbee then sees", children=[
        ui.Text(content=(
            "Each accepted message is written to the message log — and also "
            "announced as one of these events:"),
            variant="body"),
        ui.Code(content=("slack-connector.message_received\n"
                         "slack-connector.app_mentioned\n"
                         "slack-connector.thread_reply_received\n"
                         "slack-connector.dm_received"),
                language="text"),
        ui.Text(content=(
            "Every event carries the workspace, channel, author, text and the "
            "thread to answer in — so a reply lands in the same thread the "
            "person wrote in, not at the bottom of the channel."),
            variant="caption"),
        # Said plainly, because the alternative is the user building a rule that
        # can never fire and concluding the connector is broken. Verified against
        # the live platform: creating a rule on any of the four names fails with
        # "Event 'slack-connector.app_mentioned' not found" -- only OUTBOUND
        # events (the assistant sending a message) are in the catalog.
        ui.Alert(
            title="Automation triggers are not available yet",
            message=("The platform's automations catalog does not list these "
                     "four events yet, so a rule cannot be built on them "
                     "today. Nothing is lost meanwhile: every accepted message "
                     "is written to the message log, and Catch up reads Slack "
                     "directly — neither one needs an automation."),
            type="warning"),
        ui.Stack(direction="horizontal", gap=2, children=[
            ui.Button(label="Refresh",
                      on_click=ui.Call("__panel__slack", view="events")),
            ui.Button(label="Message log", variant="secondary",
                      on_click=ui.Call("__panel__slack", view="inbound")),
            ui.Button(label="Back to workspaces", variant="secondary",
                      on_click=ui.Call("__panel__slack", view="workspaces")),
        ]),
    ]))

    return ui.Stack(direction="vertical", gap=4, children=children)


def _workspaces_view(records: list[dict], load_failed: bool,
                     inbound_ready: bool = True) -> ui.Component:
    """Connected workspaces, plus the membership rules that explain emptiness.

    SKETCH -- workspaces view
      ui.Stack (v, gap=4)
        ui.Header(text="Slack Connector", level=2, subtitle=...)
        ui.Alert(...)                                   -- connection state
        ui.Section(title="Connected workspaces", children=[
          ui.DataTable(columns=[DataColumn], rows=[plain dicts])
          | ui.Empty(message=..., action=ui.Call("__panel__slack", view="connect"))
        ])
        ui.Section(title="How access works", children=[ui.Text, ui.Button, ui.Link])
    """
    children: list = [
        ui.Header(text="Slack Connector", level=2,
                  subtitle="Channels, threads and messages, by name."),
    ]

    if load_failed:
        children.append(ui.Alert(
            message=("Could not read the connected workspaces just now. The "
                     "details were logged; try refreshing."),
            type="warning"))

    # A token Slack REJECTS is the state most likely to waste the user's time:
    # the workspace row still lists a name, so nothing on screen says why every
    # action fails. Name it explicitly, and say what fixes it -- a revoked or
    # reinstalled app issues a NEW token, so reconnecting is the actual cure.
    broken = [r for r in records if r.get("status") != "ok"]
    if broken:
        which = ", ".join(
            (r.get("workspace_name") or "a workspace") for r in broken)
        children.append(ui.Alert(
            message=(
                f"Slack is refusing the token for {which}. That usually means "
                "the app was reinstalled or the token was revoked -- "
                "reinstalling issues a NEW token. Paste the current one to fix "
                "it."),
            type="warning"))

    # Inbound off is INVISIBLE otherwise: the workspace row looks healthy,
    # sending works, and the only symptom is that Webbee never reacts to
    # anything -- which reads as "the assistant is ignoring me", not as a
    # missing setting. Said here, on the screen people actually land on,
    # because the events view can only help someone who already opened it.
    if records and not inbound_ready:
        children.append(ui.Alert(
            message=("Webbee is not receiving Slack messages. Sending works, "
                     "but nothing in Slack can reach her until the signing "
                     "secret is set -- so she cannot notice a mention or "
                     "reply on her own."),
            type="warning"))
        children.append(ui.Button(
            label="Set up incoming events",
            on_click=ui.Call("__panel__slack", view="events")))

    rows = [
        {
            "workspace": r.get("workspace_name") or "Untitled workspace",
            "identity": r.get("identity") or "",
            "kind": (r.get("token_kind") or "").replace("unknown", "unrecognised"),
            "status": "Ready" if r.get("status") == "ok" else "Needs attention",
        }
        for r in records
    ]

    if rows:
        body: ui.Component = ui.DataTable(
            columns=[
                ui.DataColumn(key="workspace", label="Workspace"),
                ui.DataColumn(key="identity", label="Connected as"),
                ui.DataColumn(key="kind", label="Token"),
                ui.DataColumn(key="status", label="Status"),
            ],
            rows=rows,
        )
    else:
        # ui.Empty accepts only message / icon / action -- there is no
        # action_label, so the call to action is a Button next to it rather
        # than a label on the empty state itself.
        body = ui.Stack(children=[
            ui.Empty(
                message=("No Slack workspace is connected yet. It takes about "
                         "a minute."),
                icon="MessageSquare",
            ),
            ui.Button(label="Connect Slack",
                      on_click=ui.Call("__panel__slack", view="connect")),
        ])

    children.append(ui.Section(title="Connected workspaces", children=[body]))

    children.append(ui.Section(title="How access works", children=[
        ui.Text(content=(
            "A Slack app only reaches conversations it belongs to. Public "
            "channels can be listed without joining, but reading history or "
            "posting needs the app in the channel -- open it in Slack and type "
            "/invite @your-app."),
            variant="body"),
        ui.Text(content=(
            "Private channels need the same invite. Direct messages need none: "
            "anyone can DM the app and it can read and reply there. Message "
            "search needs a user token (xoxp-); Slack does not expose search "
            "to bot tokens at all."),
            variant="body"),
        ui.Stack(direction="horizontal", gap=2, children=[
            ui.Button(label="Refresh",
                      on_click=ui.Call("__panel__slack", view="workspaces",
                                       refresh=True)),
            ui.Button(label="Set up incoming events", variant="secondary",
                      on_click=ui.Call("__panel__slack", view="events")),
            ui.Button(label="Connect another workspace", variant="secondary",
                      on_click=ui.Call("__panel__slack", view="connect")),
        ]),
    ]))

    return ui.Stack(direction="vertical", gap=4, children=children)


@ext.panel("slack", slot="center", title="Slack", icon="MessageSquare",
           center_overlay=True, refresh="manual")
async def slack_center(ctx, **kwargs):
    """The ONE center panel. `view` picks which screen renders inside it.

    A first-time user with no token lands on the connect screen automatically:
    the default view answers "what do I do now?" instead of showing an empty
    table -- which is exactly the complaint that prompted the Notion connect
    screen ("I opened the app for the first time and can't do anything").
    """
    view = str(kwargs.get("view") or "").strip().lower()
    refresh = bool(kwargs.get("refresh"))

    records: list[dict] = []
    load_failed = False
    try:
        records = await acc.list_workspaces(ctx, refresh=refresh)
    except Exception:
        # The panel must still render: a blank screen is worse than a banner.
        # Detail goes to the audit log, never into the user-facing string.
        await ctx.log("slack panel failed to load workspaces", "error")
        load_failed = True

    if view not in ("connect", "workspaces", "events", "inbound"):
        view = "workspaces" if records else "connect"

    if view == "connect":
        return _connect_view(records)
    if view == "inbound":
        # Read defensively and render either way: this screen exists to answer
        # "has anything arrived?", and failing to a blank panel would leave that
        # question unanswered in exactly the situation where it matters most.
        rows: list[dict] = []
        stats: dict = {}
        log_failed = False
        try:
            rows = await journal.recent(ctx, limit=50)
            stats = await journal.counts(ctx)
        except Exception:
            await ctx.log("slack panel failed to read the message journal",
                          "error")
            log_failed = True
        return _inbound_view(rows, stats,
                            secret_set=await _signing_secret_is_set(ctx),
                            load_failed=log_failed)
    if view == "events":
        # Read as a boolean only. The secret's VALUE must never reach the
        # markup -- the panel says whether it is set, never what it is.
        secret_set = await _signing_secret_is_set(ctx)
        try:
            url = ctx.webhook_url("events")
        except Exception:
            url = (f"https://panel.imperal.io/v1/ext/{ext.app_id}"
                   "/webhook/events")
        # The push COUNT, not just the secret: the banner has to tell "saved"
        # apart from "actually arriving".
        pushed = 0
        try:
            pushed = int((await journal.counts(ctx)).get("from_push") or 0)
        except Exception:
            pushed = 0
        return _events_view(records, secret_set, url, pushed_count=pushed)
    return _workspaces_view(records, load_failed,
                            inbound_ready=await _signing_secret_is_set(ctx))


@ext.panel("slack_nav", slot="left", title="Slack", icon="MessageSquare",
           refresh="manual")
async def slack_nav(ctx, **kwargs):
    """Sidebar: connection state, and the one action that unblocks the user."""
    records: list[dict] = []
    try:
        records = await acc.list_workspaces(ctx)
    except Exception:
        await ctx.log("slack sidebar failed to load workspaces", "error")

    if not records:
        return ui.Stack(direction="vertical", gap=2, children=[
            ui.Text(content="No workspace connected.", variant="caption"),
            ui.Button(label="Connect Slack",
                      on_click=ui.Call("__panel__slack", view="connect")),
        ])

    healthy = [r for r in records if r.get("status") == "ok"]
    label = (records[0].get("workspace_name") or "Slack") if len(records) == 1 \
        else f"{len(records)} workspaces"

    # The journal count is read here so the sidebar can answer "is she seeing
    # anything?" at a glance. It is a cheap aggregate, and a failure to read it
    # must never cost the sidebar its buttons -- the state where nothing loads is
    # exactly the state where the user most needs a way in.
    seen = -1
    try:
        seen = int((await journal.counts(ctx)).get("total") or 0)
    except Exception:
        seen = -1

    children: list = [ui.Text(content=label, variant="body")]
    if len(healthy) != len(records):
        children.append(ui.Text(
            content="A token needs attention.", variant="caption"))
    # Two different failures, two different cures, so they are never merged
    # into one vague "needs attention": a bad token breaks SENDING, a missing
    # signing secret breaks RECEIVING. Naming the wrong one sends the user to
    # re-paste a token that was fine all along.
    if not await _signing_secret_is_set(ctx):
        children.append(ui.Text(
            content="Incoming events: off", variant="caption"))
        children.append(ui.Button(
            label="Turn on incoming", variant="secondary",
            on_click=ui.Call("__panel__slack", view="events")))
    # The message log is always offered, whatever the connection state. It is
    # the answer to "does she see my messages?", and it is also the one screen
    # that still does something useful when push is off -- Catch up needs no
    # signing secret and no automation slot.
    children.append(ui.Button(
        label=f"Message log ({seen})" if seen >= 0 else "Message log",
        variant="secondary",
        on_click=ui.Call("__panel__slack", view="inbound")))
    children.append(ui.Button(
        label="Open", on_click=ui.Call("__panel__slack", view="workspaces")))

    return ui.Stack(direction="vertical", gap=2, children=children)


def _inbound_view(rows: list[dict], stats: dict, secret_set: bool,
                  load_failed: bool = False) -> ui.Component:
    """The message log: what Webbee has actually seen, and how it got there.

    Its own view because "is Webbee aware of my messages?" is a question about
    EVIDENCE, and the honest answer is a list of messages with where each one
    came from. A status badge saying "connected" answers a different, easier
    question and is exactly what made the connector look healthy while nothing
    was arriving.

    The `source` of every row is shown deliberately. push means Slack delivered
    it; sweep means Catch up read it from Slack. Hiding that distinction would
    make a working sweep indistinguishable from a working webhook, and those
    need very different fixes when one of them stops.

    SKETCH -- inbound view
      ui.Stack (v, gap=4)
        ui.Header(text="Message log", level=2, subtitle=...)
        ui.Alert(...)                       -- how awareness is arriving
        ui.Stack (h) [ui.Badge x4]          -- totals
        ui.Stack (h) [ui.Button x3]         -- catch up / refresh / setup
        ui.List([ui.ListItem ...]) | ui.Empty
    """
    children: list = [
        ui.Header(text="Message log", level=2,
                  subtitle="Everything Webbee has seen in Slack — channels she "
                           "was added to, and direct messages."),
    ]

    if load_failed:
        children.append(ui.Alert(
            message=("The message log could not be read just now. Catch up "
                     "still works, and nothing already recorded is lost."),
            type="warning"))

    total = int(stats.get("total") or 0)
    from_push = int(stats.get("from_push") or 0)
    from_sweep = int(stats.get("from_sweep") or 0)

    # The banner names the mechanism that is actually feeding the log, because
    # "0 messages" has two completely different causes -- nobody wrote anything,
    # or nothing is arriving -- and treating them the same is how a silent
    # integration passes for a healthy one.
    if not secret_set and from_sweep and not from_push:
        children.append(ui.Alert(
            title="Awareness is running on Catch up, not on push",
            message=("Slack push delivery is off (no signing secret), so these "
                     "messages were read by Catch up. That is a real gap only "
                     "if you need instant awareness — press Catch up any time, "
                     "or turn on incoming events for push."),
            type="info"))
    elif not secret_set and not total:
        children.append(ui.Alert(
            title="Nothing recorded yet",
            message=("Push delivery is off and no sweep has run. Press Catch "
                     "up to read what is already in Slack — it needs no setup "
                     "at all."),
            type="warning"))
    elif from_push:
        children.append(ui.Alert(
            message=(f"Push delivery is working: {from_push} message(s) arrived "
                     "from Slack directly."),
            type="success"))

    children.append(ui.Stack(direction="horizontal", gap=2, wrap=True, children=[
        ui.Badge(label="messages", value=total, color="blue"),
        ui.Badge(label="direct", value=int(stats.get("dms") or 0), color="purple"),
        ui.Badge(label="mentions", value=int(stats.get("mentions") or 0),
                 color="green"),
        ui.Badge(label="from push", value=from_push,
                 color="green" if from_push else "gray"),
        ui.Badge(label="from catch up", value=from_sweep, color="gray"),
    ]))

    children.append(ui.Stack(direction="horizontal", gap=2, wrap=True, children=[
        # The primary action, and it works with zero configuration -- which is
        # the entire reason this screen can be useful today.
        ui.Button(label="Catch up now", icon="RefreshCw",
                  on_click=ui.Call("catch_up")),
        ui.Button(label="Refresh", variant="secondary",
                  on_click=ui.Call("__panel__slack", view="inbound")),
        ui.Button(label="Incoming events setup", variant="secondary",
                  on_click=ui.Call("__panel__slack", view="events")),
        ui.Button(label="Back to workspaces", variant="secondary",
                  on_click=ui.Call("__panel__slack", view="workspaces")),
    ]))

    if not rows:
        children.append(ui.Empty(
            message=("No messages recorded yet. Catch up reads the "
                     "conversations Webbee can reach and fills this in."),
            icon="MessageSquare",
            action=ui.Call("catch_up")))
        return ui.Stack(direction="v", gap=4, children=children)

    items: list = []
    for row in rows:
        author = str(row.get("user_display_name") or row.get("user_id") or "someone")
        where = str(row.get("channel_name") or row.get("channel_id") or "")
        if row.get("is_dm"):
            where = f"DM · {where}" if where else "DM"
        marks: list[str] = []
        if row.get("mention_of_bot"):
            marks.append("mentions Webbee")
        if row.get("is_thread_reply"):
            marks.append("thread reply")
        if row.get("has_files"):
            marks.append("has files")
        source = str(row.get("source") or "")
        marks.append("push" if source == "push" else "catch up")

        text = str(row.get("text_readable") or row.get("text") or "")
        items.append(ui.ListItem(
            id=str(row.get("message_key") or row.get("ts") or ""),
            title=text[:160] or "(no text)",
            subtitle=f"{author} · {where}",
            meta=f"{row.get('posted_at') or ''} · {' · '.join(marks)}",
        ))

    children.append(ui.List(items=items, searchable=True,
                            total_items=total,
                            extra_info=f"{len(items)} of {total} shown"))
    return ui.Stack(direction="v", gap=4, children=children)
