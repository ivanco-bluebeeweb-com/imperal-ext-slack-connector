# Slack Connector

Read and operate on Slack from Imperal Cloud, by NAME rather than by id: list
channels, read history and threads with mentions resolved into real names,
search, post, reply, react, pin, create channels and invite people.

## What makes this different from a raw API proxy

Slack's wire format is machine-first. This connector normalises it:

| Slack gives you | You get |
|---|---|
| `hey <@U024BE7LH> see <#C024BE7LR>` | `hey @vlad see #general` |
| `<http://example.com\|the doc>` | `the doc (http://example.com)` |
| `1690000000.123456` | `2023-07-22 04:26` (and the raw ts kept intact) |
| `{"ok": false, "error": "missing_scope"}` with HTTP **200** | a real error, with the fix |

That last row is the trap worth knowing about: **Slack reports failures as HTTP
200** with `ok: false` in the body. Anything that classifies by status code
alone reads every Slack error as a success with a suspiciously empty result.

## Setup

### 1. Create a Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
2. Name it, pick your workspace.
3. Open **OAuth & Permissions** and add the scopes below.
4. **Install to Workspace**, then copy the **Bot User OAuth Token** (`xoxb-…`).

### 2. Scopes

Add only what you need. Missing a scope produces a clear error naming it.

**Reading**

| Scope | Enables |
|---|---|
| `channels:read` | list public channels |
| `groups:read` | list private channels the app is in |
| `im:read`, `mpim:read` | list DMs and group DMs |
| `channels:history` | read public channel history |
| `groups:history`, `im:history`, `mpim:history` | read private/DM history |
| `users:read` | resolve user ids to names |
| `users:read.email` | include email addresses |

**Writing**

| Scope | Enables |
|---|---|
| `chat:write` | send, edit and delete messages |
| `reactions:write` | add and remove reactions |
| `pins:write` | pin and unpin |
| `channels:manage` | create channels, set topic/purpose |
| `channels:join` | join a public channel |
| `groups:write` | manage private channels |
| `users:read` | resolve people by name when inviting |

> After adding scopes you must **reinstall** the app. New scopes do not apply to
> an already-issued token — a scope error that "should be fixed" almost always
> means the reinstall step was skipped.

### 3. Message search needs a USER token

Slack **does not expose `search.messages` to bot tokens at all** — no scope
fixes that. For `search_messages`, add a **User Token Scope** of `search:read`,
reinstall, and paste the **User OAuth Token** (`xoxp-…`) instead. Every other
tool works fine with `xoxb-`.

### 4. Connect it

Open the Slack panel in Imperal Cloud and paste the token. It is verified
against Slack **before** it is stored, so a bad token is refused immediately
rather than failing later on every call.

Tokens are held in the Imperal Vault. The app's own store keeps only workspace
names, ids and a channel-name cache — never a credential.

## The membership rule

A Slack app only reaches conversations it **belongs to**. Public channels can be
listed without joining, but reading history or posting needs the app in the
channel:

```
/invite @your-app
```

Private channels and DMs are invisible until the app is invited. This is the
cause of most "why is it empty?" moments — `check_access` reports exactly what
is reachable and what is not.

## Multiple workspaces

A Slack token is scoped to one workspace, so multiple workspaces means multiple
tokens — one per line. Each tool takes an optional `workspace` name; omit it
when only one is connected.

## Tools

**Read** — `list_workspaces`, `list_channels`, `read_channel`, `read_thread`,
`search_messages`, `list_users`, `check_access`

**Write** — `connect_workspace`, `send_message`, `edit_message`,
`delete_message`, `react_to_message`, `pin_message`, `create_channel`,
`invite_to_channel`, `set_channel_topic`

`delete_message` is classified **destructive**: Slack has no undo for a deleted
message, so it goes through a confirmation gate.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q      # 88 tests
imperal validate                 # must be clean
```

The test suite guards the invariants that cost real debugging time:

- `ok: false` on HTTP 200 is a failure, never a success
- `ts` is identity — never coerced to a float, or replies land nowhere
- one panel per slot (two center panels silently replace each other, which
  makes buttons look dead)
- every error carries a structured code
- no token ever reaches the store, a log line, an error message or panel markup
- every write declares an `event=` so automations can trigger on it
