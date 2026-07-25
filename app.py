"""Extension declaration, secrets, lifecycle hooks.

CONNECTION MODEL -- why bot tokens and not platform OAuth.

The platform's `ext.oauth(...)` flow only knows three providers: `google`,
`microsoft` and `yahoo` (`ctx.oauth_authorize_url` raises ValueError on
anything else). Slack is not among them, so there is no platform-run OAuth
dance to hand this off to -- the same conclusion the Notion connector reached,
for the same reason.

So this connector uses Slack *app tokens*: the user creates an app at
api.slack.com/apps, grants it scopes, installs it to the workspace, and pastes
the resulting token here. A Slack token is scoped to exactly ONE workspace, so
"multiple workspaces" means multiple tokens -- the secret holds ONE TOKEN PER
LINE, one line per workspace. Workspace names and ids are discovered from
`auth.test` so the user never has to label anything by hand.

WHICH TOKEN. Slack has two kinds and the difference is user-visible:

* `xoxb-` BOT token -- acts as the app itself. The default, and what the
  Connect screen recommends: it survives the installing user leaving, and it
  only ever reaches channels the bot was invited to.
* `xoxp-` USER token -- acts as the human. Accepted because `search.messages`
  exists ONLY on user tokens (Slack does not expose search to bots at all), so
  refusing them would make "search my Slack" permanently impossible.

Both are accepted, and every tool reports which kind is in play when a
capability depends on it -- rather than failing with a scope error the user
cannot interpret.
"""

from imperal_sdk import Extension, ChatExtension

ext = Extension(
    "slack-connector",
    version="1.0.0",
    # Declared so the kernel enforces `tool.required_scopes subset-of declared`
    # instead of falling back to a WILDCARD scope grant (validator V34).
    capabilities=["slack:read", "slack:write"],
    display_name="Slack Connector",
    description=(
        "Read and operate on Slack workspaces: list channels, read channel "
        "history and threads, search messages, look up users, post and reply "
        "to messages, react, pin and manage channels across multiple "
        "workspaces."
    ),
    icon="icon.svg",
    actions_explicit=True,
)

chat = ChatExtension(
    ext,
    tool_name="slack",
    description=(
        "Slack Connector -- list channels, read message history and threads, "
        "search messages, look up people, post and reply to messages, add "
        "reactions and manage channels."
    ),
)

# Credentials never flow through chat arguments -- the user pastes them into the
# Connect screen or the platform Secrets tab (auto-added because the secret is
# declared here).
ext.secret(
    "slack_tokens",
    "Slack app token(s) -- one per line, one line per workspace. Create an app "
    "at api.slack.com/apps, add the scopes you need, install it to the "
    "workspace, then copy the Bot User OAuth Token (starts with xoxb-).",
    required=True,
    # "both" -- Panel UI writes it (Secrets manager) AND the app writes it
    # itself from the Connect screen.
    #
    # Learned on the Notion connector: with write_mode="user" the app cannot
    # store a token at all, which left the Connect screen with no action it
    # could legally call, and saving through the owner-facing route reported
    # success while the extension runtime still read nothing back -- a save
    # that looked like a no-op. With "both" the value is written through the
    # very same client that later reads it, so "saved" and "visible" cannot
    # disagree.
    write_mode="both",
    max_bytes=4096,
    rotation_hint_days=180,
)(lambda: None)


@ext.health_check
async def health_check(ctx) -> dict:
    """Liveness probe: report whether at least one Slack token is configured.

    Deliberately does NOT call Slack: a health check must stay fast and must
    not fail because a third party is briefly unreachable. It answers "is this
    app configured", not "is Slack up".
    """
    try:
        raw = await ctx.secrets.get("slack_tokens")
        count = len([ln for ln in (raw or "").splitlines() if ln.strip()])
    except Exception:
        count = 0
    return {
        "healthy": count > 0,
        "tokens_configured": count,
        "detail": ("No Slack token configured yet."
                   if count == 0 else f"{count} workspace token(s) configured."),
    }


@ext.on_install
async def on_install(ctx):
    """Make the first step traceable -- and knowable.

    A Slack token cannot be provisioned for the user, so a fresh install is
    inert by design until a token is pasted. Recording that at install time
    means "nothing works yet" shows up as an expected state in the audit log
    rather than looking like a broken deployment.
    """
    await ctx.log(
        "Slack Connector installed — awaiting a workspace token; "
        "the Connect panel walks the user through it.",
        level="info",
    )
