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
from app import ext

# The platform's own secrets manager, for anyone who prefers to paste there (or
# needs to add a SECOND workspace token on its own line).
_SECRETS_ROUTE = f"/ext/{ext.app_id}/secrets#{acc.SECRET_NAME}"
_SLACK_APPS_URL = "https://api.slack.com/apps"

# Recommended scopes. Stated explicitly because Slack's app creation screen
# offers dozens and picking wrong means reinstalling later -- and because a
# missing scope surfaces as an error the user cannot otherwise interpret.
_BOT_SCOPES = ("channels:read, groups:read, im:read, mpim:read, "
               "channels:history, groups:history, users:read, chat:write, "
               "reactions:write, pins:write")


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


def _workspaces_view(records: list[dict], load_failed: bool) -> ui.Component:
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
            "Private channels and DMs stay invisible until the app is invited. "
            "Message search needs a user token (xoxp-); Slack does not expose "
            "search to bot tokens at all."),
            variant="body"),
        ui.Stack(direction="horizontal", gap=2, children=[
            ui.Button(label="Refresh",
                      on_click=ui.Call("__panel__slack", view="workspaces",
                                       refresh=True)),
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

    if view not in ("connect", "workspaces"):
        view = "workspaces" if records else "connect"

    if view == "connect":
        return _connect_view(records)
    return _workspaces_view(records, load_failed)


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

    children: list = [ui.Text(content=label, variant="body")]
    if len(healthy) != len(records):
        children.append(ui.Text(
            content="A token needs attention.", variant="caption"))
    children.append(ui.Button(
        label="Open", on_click=ui.Call("__panel__slack", view="workspaces")))

    return ui.Stack(direction="vertical", gap=2, children=children)
