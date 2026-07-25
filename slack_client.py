"""Slack Web API helpers: one request funnel, structured errors, pagination.

THE ONE THING THAT MAKES SLACK DIFFERENT FROM MOST REST APIS
Slack signals application-level failure with **HTTP 200 and `ok: false`** in
the JSON body:

    HTTP/1.1 200 OK
    {"ok": false, "error": "channel_not_found"}

Only transport and platform-level problems (429, 5xx, occasionally 401 on a
malformed request) show up as a status code. So a status-first classifier -- the
shape the Notion client can safely use, because Notion is strict about status
codes -- would read every single Slack failure as a SUCCESS and hand a body
with no data to the caller. That is why `classify` below looks at the BODY
FIRST and the status second, and why `request` treats `ok: false` as fatal even
on 200.

Nothing in this module puts a token into a message, a log line or an error.
"""

from __future__ import annotations

SLACK_API = "https://slack.com/api"

# Slack's paginated endpoints accept up to 1000 for conversations.* and 200 for
# most others; 200 is the safe ceiling that every endpoint used here honours.
MAX_PAGE_SIZE = 200

# --- structured error codes (I-EXT-ERROR-CODE-NORMALIZED) -------------------
# Every error that reaches the user carries a stable code: it is what the
# platform error taxonomy, self-diagnosis and honest narration key on. An
# error emitted without one is stamped EXT_UNSTRUCTURED_ERROR at the dispatch
# boundary, which degrades the user's diagnosis to prose parsing.
#
# Platform taxonomy codes (imperal_sdk.chat.error_codes) are reused where the
# meaning matches exactly: PERMISSION_DENIED, RATE_LIMITED, BACKEND_5XX,
# BACKEND_TIMEOUT. Everything Slack-specific gets an app-declared code
# matching ^[A-Z][A-Z0-9_]{2,63}$. The code never appears in the message prose
# -- the two travel as separate fields.
SLACK_TOKEN_MISSING = "SLACK_TOKEN_MISSING"
SLACK_TOKEN_REJECTED = "SLACK_TOKEN_REJECTED"
SLACK_SCOPE_MISSING = "SLACK_SCOPE_MISSING"
SLACK_WRONG_TOKEN_TYPE = "SLACK_WRONG_TOKEN_TYPE"
SLACK_NOT_IN_CHANNEL = "SLACK_NOT_IN_CHANNEL"
SLACK_CHANNEL_NOT_FOUND = "SLACK_CHANNEL_NOT_FOUND"
SLACK_USER_NOT_FOUND = "SLACK_USER_NOT_FOUND"
SLACK_MESSAGE_NOT_FOUND = "SLACK_MESSAGE_NOT_FOUND"
SLACK_VALIDATION_FAILED = "SLACK_VALIDATION_FAILED"
SLACK_UNREACHABLE = "SLACK_UNREACHABLE"
SLACK_RESPONSE_NOT_JSON = "SLACK_RESPONSE_NOT_JSON"
SLACK_RESPONSE_UNEXPECTED = "SLACK_RESPONSE_UNEXPECTED"
SLACK_HTTP_ERROR = "SLACK_HTTP_ERROR"
SLACK_WORKSPACE_UNKNOWN = "SLACK_WORKSPACE_UNKNOWN"
SLACK_TARGET_NOT_FOUND = "SLACK_TARGET_NOT_FOUND"
SLACK_TARGET_AMBIGUOUS = "SLACK_TARGET_AMBIGUOUS"
SLACK_ARCHIVED = "SLACK_ARCHIVED"
# Credential STORAGE failures -- deliberately distinct from "no token
# configured". Without these, an unreadable or unwritable secret store surfaces
# as SLACK_TOKEN_MISSING: "paste your token" advice for a problem no amount of
# pasting can fix.
SLACK_SECRET_UNAVAILABLE = "SLACK_SECRET_UNAVAILABLE"
SLACK_SECRET_WRITE_FAILED = "SLACK_SECRET_WRITE_FAILED"

# Slack's `error` string is far more precise than the HTTP status (which is
# usually just 200), so it always wins.
_SLACK_ERROR_MAP = {
    # --- auth -------------------------------------------------------------
    "invalid_auth": SLACK_TOKEN_REJECTED,
    "not_authed": SLACK_TOKEN_REJECTED,
    "token_revoked": SLACK_TOKEN_REJECTED,
    "token_expired": SLACK_TOKEN_REJECTED,
    "account_inactive": SLACK_TOKEN_REJECTED,
    "no_permission": "PERMISSION_DENIED",
    "missing_scope": SLACK_SCOPE_MISSING,
    "not_allowed_token_type": SLACK_WRONG_TOKEN_TYPE,
    "ekm_access_denied": "PERMISSION_DENIED",
    # --- targets ----------------------------------------------------------
    "channel_not_found": SLACK_CHANNEL_NOT_FOUND,
    "not_in_channel": SLACK_NOT_IN_CHANNEL,
    "is_archived": SLACK_ARCHIVED,
    "user_not_found": SLACK_USER_NOT_FOUND,
    "users_not_found": SLACK_USER_NOT_FOUND,
    "message_not_found": SLACK_MESSAGE_NOT_FOUND,
    "thread_not_found": SLACK_MESSAGE_NOT_FOUND,
    "cant_update_message": "PERMISSION_DENIED",
    "cant_delete_message": "PERMISSION_DENIED",
    "name_taken": SLACK_VALIDATION_FAILED,
    "already_reacted": SLACK_VALIDATION_FAILED,
    "no_reaction": SLACK_VALIDATION_FAILED,
    "already_pinned": SLACK_VALIDATION_FAILED,
    "not_pinned": SLACK_VALIDATION_FAILED,
    # --- request shape ----------------------------------------------------
    "invalid_arguments": SLACK_VALIDATION_FAILED,
    "invalid_arg_name": SLACK_VALIDATION_FAILED,
    "invalid_array_arg": SLACK_VALIDATION_FAILED,
    "invalid_charset": SLACK_VALIDATION_FAILED,
    "invalid_form_data": SLACK_VALIDATION_FAILED,
    "invalid_post_type": SLACK_VALIDATION_FAILED,
    "missing_post_type": SLACK_VALIDATION_FAILED,
    "msg_too_long": SLACK_VALIDATION_FAILED,
    "no_text": SLACK_VALIDATION_FAILED,
    "invalid_limit": SLACK_VALIDATION_FAILED,
    "invalid_cursor": SLACK_VALIDATION_FAILED,
    "invalid_ts_latest": SLACK_VALIDATION_FAILED,
    "invalid_ts_oldest": SLACK_VALIDATION_FAILED,
    "invalid_name": SLACK_VALIDATION_FAILED,
    "restricted_action": "PERMISSION_DENIED",
    # --- throttling / platform -------------------------------------------
    "ratelimited": "RATE_LIMITED",
    "rate_limited": "RATE_LIMITED",
    "service_unavailable": "BACKEND_5XX",
    "internal_error": "BACKEND_5XX",
    "fatal_error": "BACKEND_5XX",
    "request_timeout": "BACKEND_TIMEOUT",
}

_MESSAGES = {
    SLACK_TOKEN_REJECTED: (
        "Slack rejected the token -- it may have been revoked, reinstalled, or "
        "pasted incompletely. Add a fresh token from your Slack app's OAuth "
        "page."
    ),
    SLACK_SCOPE_MISSING: (
        "The Slack app is missing the OAuth scope this action needs. Add the "
        "scope in the app's OAuth & Permissions page, then REINSTALL the app "
        "to the workspace -- new scopes only take effect after reinstalling."
    ),
    SLACK_WRONG_TOKEN_TYPE: (
        "Slack does not allow this action with the kind of token configured. "
        "Message search, in particular, works only with a user token "
        "(xoxp-) -- Slack does not expose search to bot tokens at all."
    ),
    SLACK_NOT_IN_CHANNEL: (
        "The app is not a member of that channel. In Slack, open the channel "
        "and invite the app (type /invite @your-app), then try again."
    ),
    SLACK_CHANNEL_NOT_FOUND: (
        "Slack can't see that channel. Either the name is wrong, or it is a "
        "private channel the app has not been invited to -- a bot token only "
        "sees private channels it is a member of."
    ),
    SLACK_USER_NOT_FOUND: "No such user in this Slack workspace.",
    SLACK_MESSAGE_NOT_FOUND: (
        "Slack can't find that message. It may have been deleted, or the "
        "timestamp may belong to a different channel."
    ),
    SLACK_ARCHIVED: (
        "That channel is archived, so it cannot be posted to. Unarchive it in "
        "Slack first."
    ),
    SLACK_VALIDATION_FAILED: "Slack rejected the request as invalid.",
    "PERMISSION_DENIED": (
        "Slack refused this action for this app. Bot tokens can only edit or "
        "delete their OWN messages, and workspace policy may restrict the rest."
    ),
    "RATE_LIMITED": "Slack is rate-limiting requests -- try again shortly.",
    "BACKEND_5XX": "Slack returned a server error -- try again shortly.",
    "BACKEND_TIMEOUT": "Slack took too long to respond -- try again shortly.",
    SLACK_UNREACHABLE: "Could not reach the Slack API.",
    SLACK_RESPONSE_NOT_JSON: (
        "Slack returned a response that wasn't valid JSON."
    ),
    SLACK_RESPONSE_UNEXPECTED: "Slack returned an unexpected response shape.",
    SLACK_WORKSPACE_UNKNOWN: (
        "That Slack workspace isn't connected. Check the name, or connect it "
        "on the Connect Slack screen."
    ),
    SLACK_TARGET_NOT_FOUND: "Couldn't find anything in Slack matching that.",
    SLACK_TARGET_AMBIGUOUS: (
        "Several things in Slack match that name -- say which one you mean."
    ),
    SLACK_SECRET_UNAVAILABLE: (
        "The secure store holding your Slack token could not be read just "
        "now, so the connection state is unknown. This is not a problem with "
        "your token -- try again shortly."
    ),
    SLACK_SECRET_WRITE_FAILED: (
        "The token could not be saved to the secure store, so nothing was "
        "changed. Try again shortly."
    ),
}

_RETRYABLE = {"RATE_LIMITED", "BACKEND_5XX", "BACKEND_TIMEOUT",
              SLACK_UNREACHABLE, SLACK_SECRET_UNAVAILABLE,
              SLACK_SECRET_WRITE_FAILED}


def is_retryable(code: str) -> bool:
    """Whether retrying the identical call could plausibly succeed."""
    return code in _RETRYABLE


def message_for(code: str) -> str:
    """User-facing text for a structured code (prose and code stay separate)."""
    return _MESSAGES.get(code, "The Slack request failed.")


def token_kind(token: str) -> str:
    """Classify a token by prefix: 'bot', 'user' or 'unknown'.

    Slack encodes the kind in the prefix, which lets the connector explain a
    capability gap BEFORE making a doomed call -- e.g. search needs a user
    token. A guess is never turned into a rejection: 'unknown' still gets
    tried, because Slack is the authority on its own tokens.
    """
    raw = (token or "").strip()
    if raw.startswith("xoxb-"):
        return "bot"
    if raw.startswith("xoxp-"):
        return "user"
    return "unknown"


def auth_headers(token: str) -> dict:
    """Auth + content headers. The token is never logged by this module."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


def transport_error_code(exc: BaseException) -> str:
    """Classify a transport-level failure talking to Slack.

    A timeout is a distinct, retryable condition with its own taxonomy code --
    worth separating from "host does not resolve / refused the connection",
    because the useful next step differs.
    """
    name = type(exc).__name__.lower()
    if "timeout" in name or "timedout" in name:
        return "BACKEND_TIMEOUT"
    return SLACK_UNREACHABLE


def classify(status_code: int, body) -> tuple[str, str]:
    """Map a failed Slack response onto (code, user-facing message).

    BODY FIRST, deliberately. Slack answers `{"ok": false, "error": ...}` with
    HTTP 200, so the body's `error` string is the only precise signal in the
    overwhelming majority of failures; the status is a fallback for the few
    cases that never reach application code (429, 5xx, a gateway 401).
    """
    slack_error = ""
    if isinstance(body, dict):
        slack_error = str(body.get("error") or "")

    code = _SLACK_ERROR_MAP.get(slack_error, "")
    if not code:
        if status_code == 401 or status_code == 403:
            code = SLACK_TOKEN_REJECTED
        elif status_code == 404:
            code = SLACK_TARGET_NOT_FOUND
        elif status_code == 429:
            code = "RATE_LIMITED"
        elif 500 <= status_code < 600:
            code = "BACKEND_5XX"
        elif slack_error:
            # An unmapped `ok: false` error. Slack has hundreds of these and
            # new ones appear; falling back to a generic HTTP code while
            # keeping Slack's own token in the message beats inventing a cause.
            code = SLACK_HTTP_ERROR
        else:
            code = SLACK_HTTP_ERROR

    message = _MESSAGES.get(code) or "The Slack request failed."
    if code in (SLACK_VALIDATION_FAILED, SLACK_HTTP_ERROR) and slack_error:
        # Slack's own error token names the offending condition, which is
        # exactly what makes these two fixable. It is NOT echoed for auth
        # failures, where the curated explanation is better and the raw string
        # adds nothing actionable.
        message = f"{message} Slack said: {slack_error}."
    return code, message


def retry_after_seconds(resp) -> int:
    """Slack's Retry-After header as whole seconds, or 0 when absent.

    Slack is unusually cooperative about rate limits: a 429 states how long to
    wait. Header lookup is case-insensitive because header dict casing is not
    guaranteed across HTTP clients.
    """
    headers = getattr(resp, "headers", None) or {}
    raw = ""
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            raw = str(value)
            break
    if not raw.strip():
        return 0
    try:
        return max(0, int(float(raw.strip())))
    except ValueError:
        # Retry-After may legally be an HTTP date; this app has no use for one,
        # and an unparseable value must not break error reporting.
        return 0


def fail(code: str, error: str = "") -> dict:
    """Build the module's error envelope with a stable code."""
    return {"ok": False, "code": code, "retryable": is_retryable(code),
            "error": error or message_for(code)}


async def request(ctx, method: str, path: str, token: str, *,
                  json: dict | None = None, params: dict | None = None,
                  timeout: int = 30) -> dict:
    """Call one Slack Web API method.

    Returns {"ok": True, "data": dict} or {"ok": False, "error", "code",
    "retryable"}. Every Slack call in this app funnels through here, so
    classification, timeouts and the `ok: false` handling cannot drift between
    call sites.
    """
    if not token:
        return fail(SLACK_TOKEN_MISSING,
                    "No Slack token is configured yet -- open the app's "
                    "Connect Slack screen and paste one.")

    url = f"{SLACK_API}/{path.lstrip('/')}"
    fn = getattr(ctx.http, method.lower())
    kwargs: dict = {"headers": auth_headers(token), "timeout": timeout}
    if json is not None:
        kwargs["json"] = json
    if params:
        kwargs["params"] = params

    try:
        # Explicit timeout: a hanging call must fail as a diagnosable
        # in-handler exception, not hang until the platform cancels the
        # coroutine (which surfaces to the user as an opaque INTERNAL).
        resp = await fn(url, **kwargs)
    except Exception as e:
        # The exception TYPE is a useful fact (DNS vs refused vs timeout); the
        # raw exception string is not -- it can carry hosts and internal paths.
        return fail(transport_error_code(e))

    body = resp.body
    if isinstance(body, (str, bytes, bytearray)) and body:
        try:
            body = resp.json()
        except Exception:
            if resp.status_code >= 400:
                code, message = classify(resp.status_code, None)
                return {"ok": False, "code": code, "error": message,
                        "retryable": is_retryable(code)}
            return fail(SLACK_RESPONSE_NOT_JSON)

    if resp.status_code >= 400:
        code, message = classify(resp.status_code, body)
        out = {"ok": False, "code": code, "error": message,
               "retryable": is_retryable(code)}
        # Slack states exactly how long to wait on a 429. Passing it along lets
        # the caller (or an automation retrying this) wait the RIGHT amount
        # instead of guessing a backoff.
        hint = retry_after_seconds(resp)
        if hint:
            out["retry_after"] = hint
        return out

    if not isinstance(body, dict):
        return fail(SLACK_RESPONSE_UNEXPECTED)

    # EVERY Slack response carries `ok`. A body missing the key entirely is not
    # a Slack response at all -- typically a proxy or captive portal answering
    # in Slack's place. That is a different fact from `ok: false` (which IS
    # Slack, reporting a real condition), so it gets its own code instead of
    # being classified as an ordinary API failure.
    if "ok" not in body:
        return fail(SLACK_RESPONSE_UNEXPECTED)

    # THE SLACK-SPECIFIC STEP: a 200 with ok:false is a failure. Without this,
    # every application-level Slack error would be reported as success and the
    # caller would silently read fields that are not there.
    if not body.get("ok"):
        code, message = classify(resp.status_code, body)
        out = {"ok": False, "code": code, "error": message,
               "retryable": is_retryable(code)}
        hint = retry_after_seconds(resp)
        if hint:
            out["retry_after"] = hint
        return out

    return {"ok": True, "data": body}


def _extract_cursor(data: dict) -> str:
    """Slack's next cursor lives under response_metadata, not at the top."""
    meta = data.get("response_metadata")
    if isinstance(meta, dict):
        return str(meta.get("next_cursor") or "")
    return ""


async def paginate(ctx, method: str, path: str, token: str, *,
                   params: dict | None = None, results_key: str = "channels",
                   limit: int = MAX_PAGE_SIZE, max_pages: int = 10) -> dict:
    """Follow Slack's cursor pagination until `limit` items or `max_pages`.

    Two Slack-specific details this hides from callers:

    * the next cursor is nested in `response_metadata.next_cursor`, not at the
      top level, and an EMPTY STRING (not a missing key) means "done";
    * the results key differs per method (`channels`, `members`, `messages`),
      so it is a parameter rather than a guess.

    `max_pages` is a hard stop so one tool call on a huge workspace can never
    turn into an unbounded crawl.
    """
    results: list = []
    cursor = ""
    has_more = False

    for _ in range(max_pages):
        want = min(MAX_PAGE_SIZE, max(1, limit - len(results)))
        page_params = dict(params or {})
        page_params["limit"] = want
        if cursor:
            page_params["cursor"] = cursor

        out = await request(ctx, method, path, token, params=page_params)
        if not out.get("ok"):
            return out

        data = out["data"]
        batch = data.get(results_key)
        if not isinstance(batch, list):
            return fail(SLACK_RESPONSE_UNEXPECTED,
                        "Slack returned a list response without results.")
        results.extend(batch)

        cursor = _extract_cursor(data)
        has_more = bool(cursor)
        if len(results) >= limit or not cursor:
            break

    return {"ok": True, "results": results[:limit], "has_more": has_more}
