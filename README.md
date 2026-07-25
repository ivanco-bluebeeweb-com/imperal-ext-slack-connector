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

## Incoming events (Slack → Imperal)

Everything above is Imperal calling Slack. This section is the other direction:
Slack pushing a message TO Imperal, so an automation can react to someone
writing in Slack.

### Why this needs a public endpoint

Slack's Events API does not let a client poll for "what did I miss". Slack
**pushes** each event over HTTPS to a URL you register, and that URL has to be
reachable from Slack's servers — a localhost address cannot receive events.
The connector exposes exactly one:

```
POST https://panel.imperal.io/v1/ext/slack-connector/webhook/events
```

Run `inbound_status` to print the URL for your install rather than copying the
line above — the host comes from the platform and the app id from the kernel,
so the tool is authoritative and a hardcoded URL can drift.

### 5. Turn on inbound events

1. **Slack → your app → Basic Information → App Credentials → Signing Secret.**
   Copy it.
2. In Imperal, open the Slack panel → **Incoming Slack events**, paste it into
   *Signing secret*, and save. Without it every delivery is refused — that is
   deliberate, see below.
3. **Slack → Event Subscriptions → enable**, and paste the Request URL. Slack
   immediately sends a one-off challenge; the endpoint answers it automatically,
   and the field turns *Verified*.
4. Under **Subscribe to bot events**, add:

   | Event | Delivers |
   |---|---|
   | `app_mention` | someone @-mentions the app |
   | `message.channels` | messages in public channels the app is in |
   | `message.groups` | messages in private channels the app is in |
   | `message.im` | direct messages to the app |

5. Slack will say the app must be **reinstalled** — do it. Adding events adds
   scopes, and scopes only take effect on reinstall. **Reinstalling issues a NEW
   token**, so paste the new one on the Connect screen afterwards, or every
   call starts failing with an auth error.

### Scopes inbound needs

These are *in addition* to the outbound scopes above, and they are the ones
people miss. A subscription with no matching scope is accepted by Slack's UI and
then **delivers nothing at all**, with no error anywhere.

| Scope | Without it |
|---|---|
| `app_mentions:read` | @-mentions never arrive |
| `channels:history` | public-channel messages never arrive |
| `groups:history` | **private**-channel messages never arrive |
| `im:history` | **DMs** never arrive |
| `users:read` | events arrive with a raw `U…` id instead of a name |

`groups:history` and `im:history` are separate grants: an app that reads public
channels perfectly can be completely deaf in a private channel or a DM until
those two are added **and the app reinstalled**.

### What arrives in Imperal

Four events, usable directly in the automation rule builder:

| Event | Raised when |
|---|---|
| `slack-connector.message_received` | any human message the app can see |
| `slack-connector.app_mentioned` | the message @-mentions the app |
| `slack-connector.thread_reply_received` | the message is a reply inside a thread |
| `slack-connector.dm_received` | the message is a direct message |

They are **not** mutually exclusive: a mention inside a thread raises
`message_received`, `app_mentioned` **and** `thread_reply_received`, so a rule
can trigger on whichever concept it cares about without reimplementing the
distinction.

Every event carries the same payload shape, whatever the Slack event type was:

```
workspace_id, workspace_name,
channel_id, channel_name, channel_type, is_dm,
user_id, user_display_name, user_handle,
text, text_readable,
message_ts, thread_ts, parent_message_ts,
event_type, event_id,
is_thread_reply, mention_of_bot,
reply_thread_ts, permalink
```

Fields a given event genuinely lacks are **empty, never absent** — a missing key
is a KeyError in a rule, an empty string is a condition that does not match.

### Replying in the right place

`reply_thread_ts` is the field that matters. It is pre-computed to the place a
reply belongs:

- message **in a thread** → the thread's ts, so the reply continues that thread;
- **top-level** mention in a channel → the message's own ts, so the reply opens
  a thread under it instead of shouting into the channel. That is the product
  decision here: it keeps a busy channel readable and never buries the answer
  away from the question.

Two independent ways to reply to the right place:

```
send_message(channel=<channel_id>, thread_ts=<reply_thread_ts>, text=...)
send_message(channel=<channel_id>, reply_to_last_thread=true, text=...)
```

The second exists because a timestamp has to survive a trip through an
automation prompt, and a model asked to copy `1690000000.100000` verbatim will
eventually paraphrase it — and a paraphrased ts is one Slack does not
recognise. So the connector remembers the last inbound thread per channel and
can look it back up itself.

It is **opt-in on purpose**: threading every send into the last remembered
thread would bury an unrelated "post this to #general" inside an old
conversation, and there is no undo for a message in the wrong place.

### Security, and why a missing secret refuses everything

The endpoint is public, so anything that reaches it is treated as hostile until
proven otherwise:

- **Signature.** Every delivery is verified with HMAC-SHA256 over the **raw**
  body (re-serialising the parsed JSON changes the bytes and would reject every
  request), compared with `hmac.compare_digest` — a plain `==` short-circuits on
  the first wrong byte and leaks the secret one byte at a time.
- **Replay window.** Deliveries older than five minutes are refused even with a
  valid signature; without it, one captured request stays replayable forever.
- **No signing secret = refuse everything.** Not "accept while unconfigured":
  an endpoint that trusts unsigned traffic is one anyone can use to make Webbee
  post in your Slack.
- **Failure reasons are logged, never returned.** Telling an unauthenticated
  caller *which* check it failed helps it forge a better attempt.

### Noise the connector drops for you

| Dropped | Why |
|---|---|
| the app's own messages (`bot_id` / user id match) | otherwise it answers its own answer, in public, forever |
| any other bot's messages | two bots can loop each other indefinitely |
| edits, deletions, joins, topic changes | real traffic, but nobody talking *to* the app |
| empty messages with no files | nothing to act on |
| Slack retries (`X-Slack-Retry-Num`) | Slack retries after 3s; without this the same mention is answered 2–3× |
| redelivered `event_id`s | caught by a store-backed ledger, so it survives a restart |

Dedupe **fails open**: if the ledger is briefly unreadable the event is
processed anyway. Answering twice is a smaller harm than going silent on every
message while the store is down.

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

**Inbound** — `fetch_message` (one message by ts), `fetch_thread_context`
(a whole thread, ready to reason over), `inbound_status` (is the endpoint
configured, and what to paste into Slack)

**Write** — `connect_workspace`, `connect_events`, `send_message`,
`edit_message`, `delete_message`, `react_to_message`, `pin_message`,
`create_channel`, `invite_to_channel`, `set_channel_topic`

`delete_message` is classified **destructive**: Slack has no undo for a deleted
message, so it goes through a confirmation gate.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q      # 141 tests
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
- a forged or unsigned inbound delivery is refused, and emits nothing
- the same Slack `event_id` is processed exactly once
- the app never reacts to its own message
- `is_noise` is UNPACKED, never truth-tested — it returns a tuple, and a
  non-empty tuple is always truthy, so a truth-test would silently drop 100%
  of events
