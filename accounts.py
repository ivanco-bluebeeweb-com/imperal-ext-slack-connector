"""Workspace resolution: tokens -> named workspaces, and name -> id lookup.

Two jobs, both about never making the user handle an id:

1. A Slack token is scoped to exactly ONE workspace, so "multiple workspaces"
   means multiple tokens. `auth.test` on any token returns that workspace's
   `team` name and the identity the token acts as, which is how a token gets a
   human name without asking the user to label anything.

2. Name-first targeting: the user says "#general" or "post to standup", not
   "C024BE7LR". `resolve_channel` looks names up and -- importantly -- refuses
   to guess when several match, because silently picking one and then POSTING
   to it is the expensive kind of wrong.

Tokens live only in the Vault secret. The store caches workspace NAMES, IDS and
channel lookups so panels render without hitting Slack; never a token.
"""

from __future__ import annotations

import slack_client as sc
import slack_objects as so

WORKSPACES_COLLECTION = "workspaces"
SECRET_NAME = "slack_tokens"

# How long a cached channel list stays authoritative. Channels are created and
# renamed often enough that a stale map produces "channel not found" on a
# channel the user can plainly see, so the window is deliberately short.
CHANNEL_CACHE_TTL_SECONDS = 300


def split_tokens(raw: str) -> list[str]:
    """One token per line, blanks dropped, duplicates removed.

    Blank lines and stray whitespace are tolerated: the user is pasting into a
    textarea, and a trailing newline must not read as an empty token.
    """
    seen: list[str] = []
    for line in (raw or "").splitlines():
        token = line.strip()
        if token and token not in seen:
            seen.append(token)
    return seen


async def read_tokens(ctx) -> dict:
    """Read configured tokens.

    Returns {"ok": True, "tokens": [...]} or an error envelope. A store that
    cannot be READ is reported as SLACK_SECRET_UNAVAILABLE rather than "no
    token configured": the second sends the user to paste a token again, which
    cannot fix an unreadable store, and hides a real fault behind onboarding
    advice.
    """
    try:
        raw = await ctx.secrets.get(SECRET_NAME)
    except Exception:
        await ctx.log("slack token secret could not be read", "error")
        return sc.fail(sc.SLACK_SECRET_UNAVAILABLE)
    return {"ok": True, "tokens": split_tokens(raw or "")}


async def append_token(ctx, token: str) -> dict:
    """Add one token to the stored list, keeping the existing ones.

    APPEND, never replace. Replacing would silently disconnect every other
    workspace the moment a user adds a second one -- a destructive surprise
    from an action that reads as additive.
    """
    current = await read_tokens(ctx)
    if not current.get("ok"):
        return current

    tokens = current["tokens"]
    if token in tokens:
        return {"ok": True, "tokens": tokens, "already_present": True}

    tokens = tokens + [token]
    try:
        await ctx.secrets.set(SECRET_NAME, "\n".join(tokens))
    except Exception:
        await ctx.log("slack token secret could not be written", "error")
        return sc.fail(sc.SLACK_SECRET_WRITE_FAILED)
    return {"ok": True, "tokens": tokens, "already_present": False}


async def identify(ctx, token: str) -> dict:
    """Ask Slack who this token is: workspace name/id and acting identity.

    `auth.test` is the only endpoint that needs no scopes, which makes it the
    right probe: it answers "is this token alive and where does it point" even
    for an app with a minimal scope set.
    """
    out = await sc.request(ctx, "POST", "auth.test", token)
    if not out.get("ok"):
        return out
    data = out["data"]
    return {
        "ok": True,
        # From the response HEADER, not the body: auth.test has no scopes field.
        # Without this the scope checks in check_access could never fire.
        "scopes": str(out.get("scopes") or ""),
        "workspace_name": str(data.get("team") or ""),
        "workspace_id": str(data.get("team_id") or ""),
        "identity": str(data.get("user") or ""),
        "identity_id": str(data.get("user_id") or ""),
        "bot_id": str(data.get("bot_id") or ""),
        "url": str(data.get("url") or ""),
        "token_kind": sc.token_kind(token),
    }


def _record_for(token: str, info: dict, index: int) -> dict:
    """Build the cached workspace record. NEVER stores the token itself."""
    return {
        "line": index,
        "workspace_name": info.get("workspace_name") or "Untitled workspace",
        "workspace_id": info.get("workspace_id") or "",
        "identity": info.get("identity") or "",
        "identity_id": info.get("identity_id") or "",
        # Carried through because inbound events need it: it is how the app
        # recognises ITS OWN messages coming back from Slack. auth.test already
        # returns it, and dropping it here is what would let the app answer its
        # own answer in a public channel, forever.
        "bot_id": info.get("bot_id") or "",
        "token_kind": info.get("token_kind") or sc.token_kind(token),
        "url": info.get("url") or "",
        "status": "ok",
    }


async def list_workspaces(ctx, refresh: bool = False) -> list[dict]:
    """Every configured workspace with its live status.

    Returns ONE list of records; each row carries its own status, so a single
    broken token shows up as a warning row instead of failing the whole call
    (a blank screen is worse than a labelled problem).
    """
    creds = await read_tokens(ctx)
    if not creds.get("ok"):
        # An unreadable store is not "no workspaces" -- say so in a row.
        return [{
            "line": 0,
            "workspace_name": "Unknown",
            "workspace_id": "",
            "identity": "",
            "identity_id": "",
            "token_kind": "unknown",
            "url": "",
            "status": "store_unavailable",
            "detail": creds.get("error", ""),
        }]

    tokens = creds["tokens"]
    if not tokens:
        return []

    cached: list = []
    if not refresh:
        try:
            cached = await ctx.store.list(WORKSPACES_COLLECTION)
        except Exception:
            cached = []
        if isinstance(cached, list) and len(cached) == len(tokens):
            # store.list returns Document dataclasses, and the record lives in
            # `.data` -- dict(document) raises, which would have turned a cache
            # HIT into an exception on the read path.
            rows = [getattr(doc, "data", None) or {} for doc in cached]
            if all(isinstance(row, dict) and row.get("workspace_name")
                   for row in rows):
                return rows

    records: list[dict] = []
    for index, token in enumerate(tokens):
        info = await identify(ctx, token)
        if info.get("ok"):
            records.append(_record_for(token, info, index))
        else:
            records.append({
                "line": index,
                "workspace_name": "Unusable token",
                "workspace_id": "",
                "identity": "",
                "identity_id": "",
                "token_kind": sc.token_kind(token),
                "url": "",
                "status": "error",
                "detail": info.get("error", ""),
                "code": info.get("code", ""),
            })

    try:
        # store has NO put()/clear() -- the real API is set("collection/doc_id")
        # for an upsert and delete(collection, doc_id) for removal. Calling the
        # names that do not exist raised AttributeError into the except below,
        # so the cache silently NEVER populated: every call re-ran auth.test per
        # token. It looked fine because the failure only logged a warning.
        stale = []
        try:
            stale = await ctx.store.list(WORKSPACES_COLLECTION)
        except Exception:
            stale = []
        keep = {str(record["line"]) for record in records}
        for doc in stale or []:
            doc_id = getattr(doc, "id", None) or ""
            if doc_id and doc_id not in keep:
                await ctx.store.delete(WORKSPACES_COLLECTION, doc_id)
        for record in records:
            await ctx.store.set(
                f"{WORKSPACES_COLLECTION}/{record['line']}", record)
    except Exception:
        # The cache is an optimisation; failing to write it must not fail the
        # call the user actually asked for.
        await ctx.log("workspace cache could not be updated", "warn")

    return records


async def resolve_workspace(ctx, name: str = "") -> dict:
    """Pick which workspace to act in and hand back its token.

    Returns {"ok": True, "token", "workspace"} or an error envelope. With one
    workspace connected the name is optional -- asking "which workspace?" when
    there is only one is pure friction.
    """
    creds = await read_tokens(ctx)
    if not creds.get("ok"):
        return creds

    tokens = creds["tokens"]
    if not tokens:
        return sc.fail(
            sc.SLACK_TOKEN_MISSING,
            "No Slack token is configured yet. Create an app at "
            "api.slack.com/apps, install it to your workspace, then paste its "
            "Bot User OAuth Token on the Connect Slack screen.")

    wanted = (name or "").strip().lower()

    if not wanted:
        if len(tokens) == 1:
            info = await identify(ctx, tokens[0])
            if not info.get("ok"):
                return info
            return {"ok": True, "token": tokens[0],
                    "workspace": _record_for(tokens[0], info, 0)}
        records = await list_workspaces(ctx)
        names = [r.get("workspace_name", "") for r in records
                 if r.get("status") == "ok"]
        return sc.fail(
            sc.SLACK_WORKSPACE_UNKNOWN,
            "Several Slack workspaces are connected -- say which one: "
            + ", ".join(n for n in names if n) + ".")

    records = await list_workspaces(ctx)
    for record in records:
        candidates = {
            str(record.get("workspace_name", "")).strip().lower(),
            str(record.get("workspace_id", "")).strip().lower(),
        }
        if wanted in {c for c in candidates if c}:
            line = int(record.get("line", 0))
            if 0 <= line < len(tokens):
                if record.get("status") != "ok":
                    return sc.fail(
                        record.get("code") or sc.SLACK_TOKEN_REJECTED,
                        record.get("detail") or "")
                return {"ok": True, "token": tokens[line], "workspace": record}

    known = ", ".join(r.get("workspace_name", "") for r in records
                      if r.get("workspace_name"))
    return sc.fail(
        sc.SLACK_WORKSPACE_UNKNOWN,
        f"No connected Slack workspace called '{name}'."
        + (f" Connected: {known}." if known else ""))


async def _all_conversations(ctx, token: str, limit: int = 1000) -> dict:
    """Every conversation the token can see, across all four kinds.

    `types` must be requested EXPLICITLY: conversations.list defaults to
    public_channel only, so a private channel the app was invited to would look
    like it does not exist.
    """
    return await sc.paginate(
        ctx, "GET", "conversations.list", token,
        params={
            "types": "public_channel,private_channel,mpim,im",
            "exclude_archived": "false",
        },
        results_key="channels", limit=limit, max_pages=10,
    )


async def resolve_channel(ctx, token: str, reference: str) -> dict:
    """Resolve '#general' / 'general' / 'C123' to a conversation.

    Returns {"ok": True, "id", "name", "kind", "is_member", "is_archived"} or
    an error envelope.

    An id short-circuits the lookup. Otherwise the name is matched
    case-insensitively, and an AMBIGUOUS name is refused rather than guessed:
    the caller may be about to post, and posting to the wrong channel is not
    quietly recoverable.
    """
    ref = so.normalize_channel_ref(reference)
    if not ref:
        return sc.fail(sc.SLACK_VALIDATION_FAILED,
                       "Name a channel, for example #general.")

    if so.looks_like_channel_id(ref):
        out = await sc.request(ctx, "GET", "conversations.info", token,
                               params={"channel": ref})
        if not out.get("ok"):
            return out
        channel = out["data"].get("channel")
        if not isinstance(channel, dict):
            return sc.fail(sc.SLACK_RESPONSE_UNEXPECTED)
        return {
            "ok": True,
            "id": str(channel.get("id") or ref),
            "name": so.channel_name(channel),
            "kind": so.channel_kind(channel),
            "is_member": bool(channel.get("is_member")),
            "is_archived": bool(channel.get("is_archived")),
        }

    listing = await _all_conversations(ctx, token)
    if not listing.get("ok"):
        return listing

    wanted = ref.lower()
    matches = [c for c in listing["results"]
               if isinstance(c, dict)
               and so.channel_name(c).lower() == wanted]

    if not matches:
        # Substring fallback: "standup" should find "daily-standup" rather than
        # claiming the channel does not exist.
        matches = [c for c in listing["results"]
                   if isinstance(c, dict)
                   and wanted in so.channel_name(c).lower()]

    if not matches:
        return sc.fail(
            sc.SLACK_CHANNEL_NOT_FOUND,
            f"No channel matching '{reference}' is visible to this app. "
            "A bot token only sees private channels it has been invited to.")

    if len(matches) > 1:
        names = ", ".join("#" + so.channel_name(c) for c in matches[:8])
        return sc.fail(
            sc.SLACK_TARGET_AMBIGUOUS,
            f"Several channels match '{reference}': {names}. "
            "Say which one you mean.")

    channel = matches[0]
    return {
        "ok": True,
        "id": str(channel.get("id") or ""),
        "name": so.channel_name(channel),
        "kind": so.channel_kind(channel),
        "is_member": bool(channel.get("is_member")),
        "is_archived": bool(channel.get("is_archived")),
    }


async def resolve_user(ctx, token: str, reference: str) -> dict:
    """Resolve '@vlad' / 'vlad' / 'U123' / an email to a workspace member.

    Returns {"ok": True, "id", "name", "email", "is_bot"} or an error envelope.
    Ambiguity is refused for the same reason as channels: the caller may be
    about to DM a stranger.
    """
    ref = so.normalize_user_ref(reference)
    if not ref:
        return sc.fail(sc.SLACK_VALIDATION_FAILED,
                       "Name a person, for example @vlad.")

    if so.looks_like_user_id(ref):
        out = await sc.request(ctx, "GET", "users.info", token,
                               params={"user": ref})
        if not out.get("ok"):
            return out
        user = out["data"].get("user")
        if not isinstance(user, dict):
            return sc.fail(sc.SLACK_RESPONSE_UNEXPECTED)
        return _user_result(user)

    if "@" in ref and "." in ref.split("@")[-1]:
        # An email is an exact key, and Slack has a dedicated endpoint for it --
        # far more reliable than scanning the member list for a display name.
        out = await sc.request(ctx, "GET", "users.lookupByEmail", token,
                               params={"email": ref})
        if out.get("ok"):
            user = out["data"].get("user")
            if isinstance(user, dict):
                return _user_result(user)
        elif out.get("code") != sc.SLACK_USER_NOT_FOUND:
            return out

    listing = await sc.paginate(ctx, "GET", "users.list", token,
                                results_key="members", limit=1000,
                                max_pages=10)
    if not listing.get("ok"):
        return listing

    wanted = ref.lower()
    members = [m for m in listing["results"] if isinstance(m, dict)]

    exact = [m for m in members
             if wanted in {str(m.get("name") or "").lower(),
                           so.user_display_name(m).lower()}]
    matches = exact or [m for m in members
                        if wanted in so.user_display_name(m).lower()]

    if not matches:
        return sc.fail(sc.SLACK_USER_NOT_FOUND,
                       f"No one in this workspace matches '{reference}'.")
    if len(matches) > 1:
        names = ", ".join(so.user_display_name(m) for m in matches[:8])
        return sc.fail(
            sc.SLACK_TARGET_AMBIGUOUS,
            f"Several people match '{reference}': {names}. "
            "Say which one you mean.")
    return _user_result(matches[0])


def _user_result(user: dict) -> dict:
    profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
    return {
        "ok": True,
        "id": str(user.get("id") or ""),
        "name": so.user_display_name(user),
        "email": str((profile or {}).get("email") or ""),
        "is_bot": bool(user.get("is_bot")),
        "title": str((profile or {}).get("title") or ""),
        "tz": str(user.get("tz") or ""),
    }


async def name_maps(ctx, token: str) -> tuple[dict, dict]:
    """(user id -> name, channel id -> name) for rendering message markup.

    Best effort by design: if either lookup fails, rendering degrades to raw
    ids rather than failing the read the user asked for. A channel history with
    unresolved mentions is still useful; an error instead of the history is not.
    """
    users: dict = {}
    channels: dict = {}

    listing = await sc.paginate(ctx, "GET", "users.list", token,
                                results_key="members", limit=1000, max_pages=5)
    if listing.get("ok"):
        users = so.user_name_map(listing["results"])

    conversations = await _all_conversations(ctx, token)
    if conversations.get("ok"):
        channels = so.channel_name_map(conversations["results"])

    return users, channels
