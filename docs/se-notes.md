# SE Setup Notes — Deviations & Gaps in the Blog Walkthrough

Running notes for Okta APAC SE Summit attendees, captured while working through
[Securing Amazon Bedrock AgentCore Agents with Auth0 for AI Agents](https://auth0.com/blog/securing-amazon-bedrock-agentcore-agents-auth0-for-ai-agents/)
against this repo. The blog assumes context/accounts it never states explicitly — this
doc calls those out as they're found, plus any other setup friction.

## Missing environment prerequisites

The blog does not state up front that you need **four separate accounts/environments**
provisioned before starting. Get all of these ready first:

1. **Okta demo org** — use a **Workforce (starter) template** org, not a generic Okta
   Developer org. Needed because the demo's `getOktaGroups` tool calls Okta's native
   Users/Groups API directly (`registerAgent/agentcore_agent.py`) — it is not satisfied
   by anything in Auth0 itself. Populate at least one test user with group memberships,
   since that's what the demo query actually returns.
2. **Auth0 demo tenant** — separate from the Okta org above. Handles login, Token Vault,
   CIBA, and the AgentCore `customJWTAuthorizer`.
3. **AWS account** — for Bedrock AgentCore Runtime/Gateway, DynamoDB, Secrets Manager,
   ECR. Regular account credentials are sufficient; no need to provision a dedicated IAM
   User for this (see follow-up note once confirmed).
4. **Auth0 FGA account** — separate product/account from the Auth0 tenant, used for the
   fine-grained authorization check in `agentcore_agent.py`.

## "Creating an enterprise connection" step is under-specified

The blog's enterprise connection step links out to generic Auth0 docs and gives no
concrete values, which makes it non-obvious what to actually configure. Clarified:

- **Purpose**: not the login mechanism. It's a secondary OIDC link from Auth0 to the
  Okta org, whose only job is letting Auth0's **Token Vault** cache a real Okta
  access+refresh token for the logged-in user. The `getOktaGroups` tool is the sole
  consumer of that cached token.
- **OIDC Discovery URL** = your Okta org's own discovery doc:
  `https://{your-okta-domain}/.well-known/openid-configuration`. This must be the same
  Okta org you'll later set as `OKTA_DOMAIN`.
- **Client ID / Secret** = credentials from a **new OIDC app created in Okta itself**
  (Okta Admin Console → Applications → Create App Integration → OIDC, "Web
  Application", grant type Authorization Code) — not an Auth0 application.
- **Redirect URI** on that Okta app = the Auth0 IdP callback shown by Auth0's connection
  creation UI (`https://{AUTH0_DOMAIN}/login/callback`).
- Must enable the **Refresh Token grant type** / `offline_access` scope on the Okta app
  — without a refresh token, Token Vault has nothing to redeem later. This matches the
  blog's easily-missed "ensure `offline_scope` is enabled" instruction.

## AWS credentials: blog assumes a static IAM User; doesn't work with SSO-only accounts

The blog's "Setup AWS credentials" step (Part 2) says AWS credentials are required "to
enable communication between the web app and the Amazon Bedrock AgentCore agent" — that
framing is misleading. In this repo, AWS credentials in the web app are used for exactly
one thing: the explicit `boto3.resource("dynamodb", ...)` call in `app.py` that
reads/writes the session table (`session_id`, `access_token`, `federated_token`,
`profile`). The actual call to the AgentCore Runtime's `/invocations` endpoint is
authenticated with the Auth0 access token as a Bearer header, validated by the Runtime's
`customJWTAuthorizer` — no AWS SigV4 signing, no AWS credentials involved on that hop.

Why this matters: our Okta AWS environment uses SSO and cannot create IAM Users — only
temporary STS credentials (Access Key ID, Secret Access Key, Session Token) via
`aws sso login`. The repo's original `boto3.resource(...)` call only accepted
`aws_access_key_id`/`aws_secret_access_key`, not `aws_session_token`, so temporary SSO
credentials would be rejected there (boto3 requires all three together for temp creds).
`region_name` was also hardcoded to `"us-east-1"`, ignoring the `AWS_REGION` env var
entirely.

**Change made**: in `app.py`, the DynamoDB resource call now reads
`aws_session_token=os.getenv("AWS_SESSION_TOKEN")` and
`region_name=os.getenv("AWS_REGION")` instead of a hardcoded region and a two-credential
pair. This lets plain SSO-derived temporary credentials work as drop-in `.env` values,
with no change needed anywhere else — the deployment script
(`registerAgent/agentcore_deployment.py`) and the agent's own DynamoDB/Secrets Manager
clients never override credentials explicitly, so they already pick up
`AWS_SESSION_TOKEN` from boto3's default credential provider chain.

Caveat: temporary SSO credentials expire (session-policy dependent, typically 1–12
hours). When they do, re-run `aws sso login`, get a fresh Access Key/Secret/Session
Token triple, and update `.env` — there's no way to hand boto3 the SSO start URL
directly at the `resource()`/`client()` level, so this refresh has to happen manually
unless/until we switch to profile-based SSO resolution (`boto3.Session(profile_name=...)`)
instead.

**Also flagged**: `env.template` doesn't list `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
or `AWS_SESSION_TOKEN` at all, despite `app.py` reading all three — needs updating so the
template actually reflects what the app requires.

## Dropped AWS Secrets Manager entirely — everything moves to `registerAgent/.env`

For this internal enablement lab, decided the Secrets Manager setup step (a 5th account
concern on top of Okta/Auth0/AWS/FGA) isn't worth the setup time — the web app's `.env`
already holds equivalent client IDs/secrets, so putting the agent's secrets in
`registerAgent/.env` too is no less secure in this context and meaningfully faster to set
up. This removes the need to ever create/populate an AWS Secrets Manager secret.

**What changed in `agentcore_agent.py`**:
- Deleted `SECRETS_ARN`, `load_managed_secrets()`, and `MANAGED_SECRETS` entirely — no more
  `boto3` Secrets Manager client at all.
- All 14 values it used to pull via `MANAGED_SECRETS.get(...)` now come from
  `os.getenv(...)` instead, same keys, same defaults: `AUTH0_DOMAIN_CIBA`,
  `CIBA_CLIENT_ID`, `CIBA_CLIENT_SECRET`, `CIBA_SCOPE`, `CIBA_BINDING_MESSAGE`,
  `FGA_API_TOKEN_ISSUER`, `FGA_API_AUDIENCE`, `FGA_CLIENT_ID`, `FGA_CLIENT_SECRET`,
  `FGA_API_SCHEME`, `FGA_API_URL`, `FGA_STORE_ID`, `FGA_MODEL_ID`,
  `MCP_GATEWAY_URL`, `OKTA_DOMAIN`. (FGA names updated since — see below. The three CIBA
  identity vars were later removed entirely — see below.)
- These reach the deployed container via AgentCore Runtime's native
  `environment_variables` config (set at deploy time), **not** via a `.env` file copied
  into the Docker image — `registerAgent/.env` stays local-only and is excluded from the
  build via a new `.dockerignore`.

**What changed in `agentcore_deployment.py`**: reads the same 14 keys (plus
`SESSION_TABLE_NAME` and `AWS_REGION`, see below) from its own `load_dotenv()`-loaded
`.env`, and passes them to both `agentcore_runtime.configure(...)` calls via
`environment_variables={...}`. AWS injects these into the container's process
environment at startup, so `os.getenv` in `agentcore_agent.py` picks them up identically
whether running locally off `.env` or deployed via Runtime config.

## DynamoDB table name and region were hardcoded in the agent

Separately found while making the above change: `get_dynamodb_table()` in
`agentcore_agent.py` hardcoded the table name to the literal `"auth0_agentcore_agent"`
(the `if not table_name` check below it was dead code — `table_name` was always a
truthy literal) and defaulted `region` to a hardcoded `"us-east-1"`. Neither ever
consulted an env var, so setting `SESSION_TABLE_NAME` in any `.env` had no effect on the
agent side — only on the web app side.

**Change made**: `get_dynamodb_table()` now reads `table_name = os.getenv("SESSION_TABLE_NAME")`
(raises if unset, same as before) and `region_name=os.getenv("AWS_REGION", "us-east-1")`.
Both are added to the set of values `agentcore_deployment.py` reads from `.env` and pushes
via `environment_variables` at deploy time — so the web app and the agent now agree on the
same table name and region as long as both `.env` files use the same values.

## DEFERRED — DynamoDB is mostly a plumbing workaround, not a real credential store

Not implemented yet (sizable change, parked for time-boxing) — but worth recording now
so the reasoning isn't lost. Traced every read/write of the session table and found
DynamoDB is doing three unrelated jobs, only one of which actually needs persisted
storage:

**Write side** — `app.py:store_session_data` ([app.py:202-238](../app.py#L202-L238))
writes one row per `session_id` at login/connect-account time: `user_id`, `email`,
`name`, `picture`, `federated_token`, `connection_name`, `connected_accounts`, optionally
`refresh_token`/`access_token`.

**Read side** — three lookups in `agentcore_agent.py`, each pulling a different field:

1. `invokeCiba` ([agentcore_agent.py:139-148](../registerAgent/agentcore_agent.py#L139-L148))
   — looks up the row to get `user_id` (Auth0 `sub`) for CIBA's `login_hint.sub`. Not a
   secret, not a delegation concern — just a value the web app already has
   (`session_store["profile"]["user_id"]`, populated from `userinfo.get("sub")` at login,
   [app.py:324](../app.py#L324)) but never forwards in the invoke request body.
   **Desired fix**: add `"user_id"` to the JSON body `app.py` posts to `/invocations`
   ([app.py:513-519](../app.py#L513-L519)), same as `email`/`prompt`/`dynamoID` today.
   Removes this DB read entirely — no exchange or header trick needed, it's just a
   non-sensitive identifier the web app already holds.
2. `getOktaGroups` ([agentcore_agent.py:293-297](../registerAgent/agentcore_agent.py#L293-L297))
   — looks up `federated_token`, the Okta token Token Vault cached earlier. This is the
   one legitimate use of storage: the value is fetched by the web app at a different time
   than it's consumed by the agent. **Explicitly deferred for now** — the real fix here
   is having the agent redeem it live via Auth0's
   `urn:auth0:params:oauth:grant-type:token-exchange:federated-connection-access-token`
   grant at `/oauth/token` (confirmed stateless, doesn't require the original login
   client — [Auth0 docs](https://auth0.com/docs/secure/call-apis-on-users-behalf/token-vault/access-token-exchange-with-token-vault)),
   using the inbound Auth0 access token as `subject_token`. Requires standing up a new
   Auth0 "Custom API Client" linked to the resource server matching `AUTH0_AUDIENCE`, plus
   rewiring both `app.py` (drop the Token Vault call + session write for this field) and
   `getOktaGroups` (call the exchange inline instead of reading a cached value). Real
   scope, not a quick tweak — left as-is on purpose.
3. Entrypoint itself ([agentcore_agent.py:369-371](../registerAgent/agentcore_agent.py#L369-L371))
   — looks up `access_token` and stashes it in a module global, reused later to build the
   outbound `Authorization` header for the MCP Gateway call
   ([`create_transport()`](../registerAgent/agentcore_agent.py#L342-L346)). This is the
   same Auth0 access token already sitting in this exact request's inbound `Authorization`
   header ([app.py:504](../app.py#L504)) — DynamoDB is being used to smuggle a value into
   the agent process that arrived on the wire moments earlier, purely because the
   entrypoint only receives the JSON `payload`, not the raw request headers.
   **Desired fix, and why not just add it to the JSON body**: putting `access_token` in
   the body works (same TLS-encrypted request, no new transport exposure) but the
   entrypoint currently logs the *entire* payload verbatim
   ([agentcore_agent.py:357-358](../registerAgent/agentcore_agent.py#L357-L358)), which
   would put the raw token into CloudWatch logs on every invocation. The cleaner fix is
   AgentCore Runtime's native `requestHeaderAllowlist` mechanism — the entrypoint accepts
   a `context: RequestContext` parameter and reads `context.request_headers["Authorization"]`
   directly, never touching the payload/logs at all
   ([AWS docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-header-allowlist.html)).
   Requires: adding `Authorization` to a `requestHeaderAllowlist` in the runtime config
   (`agentcore_deployment.py`'s `configure()` call), changing the entrypoint signature to
   `def strands_agent_bedrock(payload, context: RequestContext)`, and reading the token off
   `context` instead of the DynamoDB lookup.

**Net effect if #1 and #3 are done** (leaving #2 deferred): DynamoDB drops to a single
purpose — caching `federated_token` — rather than three unrelated ones. Combining all
three would remove the DynamoDB dependency from this lab entirely.

## Blog's DynamoDB table name ("bedrock-sessions") only works because of the fix above

The blog has you create a table literally named `bedrock-sessions`. Before the
`SESSION_TABLE_NAME` hardcoding fix (above), that name — or any name — would not have
worked on the agent side: `agentcore_agent.py` hardcoded `"auth0_agentcore_agent"`
regardless of what you named the table or set in any env var. Now that both sides read
`SESSION_TABLE_NAME` from env, naming it `bedrock-sessions` (or anything else) works, as
long as `SESSION_TABLE_NAME` is set to the same value in **both** `.env` files (root and
`registerAgent/`).

Also fixed: root `env.template` never listed `SESSION_TABLE_NAME` at all despite `app.py`
reading it — added it under "Agent Core Configuration". Without it, the web app would
silently skip session persistence (`store_session_data`'s `if not SESSION_TABLE_NAME`
guard just logs and returns — no error) and `/chat` would later fail with "Session
expired or invalid".

**Still outstanding, not yet fixed**: `env.template` still doesn't list
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`, flagged earlier in the
SSO-credentials section above — same category of gap, not yet closed.

## Remaining hardcoded region: the AgentCore invocation URL itself

Separate from DynamoDB — found while auditing hardcoded regions generally.
`app.py`'s call to the Bedrock AgentCore Runtime `/invocations` endpoint was hardcoded to
`https://bedrock-agentcore.us-east-1.amazonaws.com/...` regardless of `AWS_REGION`. Harmless
if you deploy everything in `us-east-1` (which the rest of the repo assumes throughout —
Dockerfile, both env templates default to it), but would silently break if the Runtime is
ever deployed to a different region.

**Change made**: added a module-level `AWS_REGION = os.getenv("AWS_REGION", "us-east-1")`
in `app.py`, reused for both the DynamoDB resource's `region_name` and the invocation URL
(`f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com/..."`), so there's one source of
truth for region on the web app side instead of two separate `os.getenv`/hardcoded values.

## FGA env var names renamed to match the FGA store's own config output

`registerAgent/.env`'s original FGA variable names (`FGA_API_ISSUER`, `FGA_API_HOST`,
`FGA_AUTHORIZATION_MODEL_ID`) didn't match what the Auth0 FGA store setup screen actually
outputs (`FGA_API_TOKEN_ISSUER`, `FGA_API_URL`, `FGA_MODEL_ID`), so values couldn't be
pasted straight across — small but real friction for anyone doing this lab.

**Change made**: renamed in `agentcore_agent.py`, `agentcore_deployment.py`'s pushed env
var list, and `env.sample`:
- `FGA_API_ISSUER` → `FGA_API_TOKEN_ISSUER`
- `FGA_AUTHORIZATION_MODEL_ID` → `FGA_MODEL_ID`
- `FGA_API_HOST` → `FGA_API_URL` (see nuance below)
- `FGA_API_AUDIENCE`, `FGA_CLIENT_ID`, `FGA_CLIENT_SECRET`, `FGA_STORE_ID` unchanged —
  already matched.

**One nuance on `FGA_API_HOST` → `FGA_API_URL`**: FGA's output gives a full URL (e.g.
`https://api.us1.fga.dev`), but the OpenFGA Python SDK's `ClientConfiguration` wants
`api_scheme` and `api_host` as two separate params, host without the scheme prefix. So
`agentcore_agent.py` now reads `FGA_API_URL` and strips `https://`/`http://` off it
in code (same pattern already used elsewhere in this file for `CIBA_AUTH0_DOMAIN`) to get
the bare host for `api_host` — the env var itself now matches FGA's copy-paste output,
even though the SDK still needs it split internally.

`FGA_API_SCHEME` (default `"https"`) is **not** part of FGA's output and was kept
unchanged, purely because the SDK needs scheme and host as separate params.

## CIBA env vars: removed the redundant identity vars, and fixed two dead-code bugs

Auditing whether the CIBA env vars were actually read turned up two separate problems.

**1. `AUTH0_DOMAIN_CIBA`, `CIBA_CLIENT_ID`, `CIBA_CLIENT_SECRET` were pure duplicates.**
Per the blog, CIBA reuses the *same* Auth0 application as the main login/JWT-authorizer
flow (ticking the Token Vault + CIBA grant checkboxes on that one app) — there's no
separate CIBA app or tenant. So these three vars always held the exact same values as
`AUTH0_DOMAIN`/`AUTH0_CLIENT_ID`/`AUTH0_CLIENT_SECRET`, just duplicated under different
names for no reason.

**Change made**: `agentcore_agent.py` now reads `AUTH0_DOMAIN`/`AUTH0_CLIENT_ID`/
`AUTH0_CLIENT_SECRET` directly for CIBA (internal variable names `CIBA_AUTH0_DOMAIN`/
`CIBA_CLIENT_ID`/`CIBA_CLIENT_SECRET` kept as-is for readability at the call sites, just
sourced from the shared vars now). Removed the three redundant vars from `env.sample`.
`AUTH0_CLIENT_SECRET` didn't previously exist in `registerAgent/env.sample` at all (only
`AUTH0_DOMAIN`/`AUTH0_CLIENT_ID`/`AUTH0_AUDIENCE` did) — added it. Also updated
`agentcore_deployment.py`'s pushed `environment_variables` list: dropped the three
duplicates, added `AUTH0_DOMAIN`/`AUTH0_CLIENT_ID`/`AUTH0_CLIENT_SECRET` so the *deployed*
container actually has them (previously only used locally by the deploy script itself,
for the JWT authorizer config — never forwarded into the Runtime).

**2. `CIBA_SCOPE` and `CIBA_BINDING_MESSAGE` were read into variables and then ignored.**
The actual `/bc-authorize` request hardcoded literal values (`"openid profile"` and
`"Please approve the password-reset request"`) instead of using the env-derived
`CIBA_SCOPE`/`DEFAULT_BINDING_MESSAGE` variables — setting either env var changed
nothing. Confirmed `binding_message` is a real, user-visible value: Auth0's CIBA docs
confirm it's relayed through to the Consent API and displayed on the user's push-approval
device, used to visually bind the request to what they initiated — not just internal
bookkeeping ([Auth0 docs](https://auth0.com/docs/get-started/authentication-and-authorization-flow/client-initiated-backchannel-authentication-flow/mobile-push-notifications-with-ciba)).

Separately, `invokeCiba`'s own `scope`/`binding_message` tool arguments (meant to let the
agent pass per-call values) were *also* ignored by the same hardcoding.

**Change made**: the `/bc-authorize` payload now uses `"scope": scope or CIBA_SCOPE` and
`"binding_message": binding_message or DEFAULT_BINDING_MESSAGE` — the tool's own
call-time arguments win if the agent supplies them, otherwise it falls back to the
`.env`-configured value, otherwise that var's own hardcoded default
(`"openid profile"` / `"RESET PASSWORD FLOW"`) applies as before.

## `AUTH0_SCOPE` in `env.template` is dead — nothing reads it

Same category of finding as `BEARER_TOKEN` above, just for scopes instead of a token.
`env.template` has shipped an `AUTH0_SCOPE=invoke:gateway read:gateway` line this whole
time, but no code anywhere calls `os.getenv("AUTH0_SCOPE")`. The scopes actually
requested at login are **hardcoded literals**, in two slightly different places:
- `app.py:37` — `"openid profile email offline_access okta.users.read read:me:connected_accounts"`
- `connect_account.py:60` — the same list, minus `okta.users.read`

Setting `AUTH0_SCOPE` to anything in `.env` has zero effect. Not fixed (no one asked for
scope-driven config here) — just documented so nobody burns time debugging a scope
change that silently does nothing.

## Creating a custom API/Resource Server for `AUTH0_AUDIENCE` — required, never documented

The blog never has you create this, and it isn't the MyAccount API (that's a separate,
built-in, fixed-identifier system API — `https://{yourDomain}/me/` — activated via a
toggle, used only for Token Vault/Connected Accounts; unrelated to this). `AUTH0_AUDIENCE`
is a genuinely custom API you must create yourself, and it's required: without it, Auth0
won't mint a JWT carrying that value as the `aud` claim, and the AgentCore Runtime's
`customJWTAuthorizer` (which checks `iss`/`aud`/`client_id` — see
`registerAgent/agentcore_deployment.py:71-75` and `:120-124`) will reject every request.

**Steps**:
1. Auth0 Dashboard → Applications → APIs → Create API. Pick any identifier you like —
   it doesn't need to resolve to a real URL (e.g. `https://agentcore-agent`).
2. No scopes are required on it — the `customJWTAuthorizer` only checks audience/issuer/
   client, never scope. (Scope-based authorization in this demo happens separately, via
   the FGA check in `agentcore_agent.py`'s `main()`.)
3. No explicit "link this app to the API" step is needed either — that (Client Grants /
   "Machine to Machine Applications" tab) is specifically an M2M/Client Credentials
   requirement. The web app logs in via Authorization Code flow, which can request a
   token for any existing API via the `audience` parameter without a separate grant, as
   long as the API exists with a matching identifier.
4. Set the **exact same identifier** as `AUTH0_AUDIENCE` in **both** `.env` files — root
   `.env` (so the web app requests a token scoped to it, `app.py:31,50,73`) and
   `registerAgent/.env` (so `agentcore_deployment.py`'s `allowedAudience` accepts it,
   `:75`/`:124`). They must match exactly, character for character.

**Done for this lab**: created the API with identifier `https://agentcore.summit`, set
as `AUTH0_AUDIENCE` in both `.env` files. Also added scopes `invoke:gateway` and
`read:gateway` to the API.

**TODO, revisit later**: those two scopes aren't used by anything yet — per the
`AUTH0_SCOPE` dead-env-var finding above, nothing in the codebase currently requests
scoped tokens against this API at all (the hardcoded scope literals in `app.py`/
`connect_account.py` are all Auth0-native scopes like `openid`/`profile`/
`okta.users.read`, nothing `*:gateway`). Come back to this if/when the AgentCore Gateway
leg starts doing real scope-based authorization — right now defining them on the API is
a no-op.

## `deployAgentCore` hit three real bugs on first run, plus five dead dependencies

Running the deploy script for the first time on macOS surfaced a chain of environment and
packaging issues, none of them things you did wrong:

1. **`pip: command not found`** — macOS ships no bare `pip`/`python` on PATH by default,
   only `python3` (and that's Apple's ancient Command Line Tools Python, 3.9.6, pip
   21.2.4). Fixed in `deployAgentCore`/`runLocalApp`: both now resolve a real Python 3.10+
   explicitly (Homebrew's `python3.12` if present, falling back down through 3.11/3.10/
   plain `python3`), create a dedicated `.venv` on first run if one doesn't exist yet,
   and activate it before installing anything — so `pip`/`python` inside that venv are
   always the right ones, and installs never touch system/CLT Python.
2. **`ModuleNotFoundError: No module named 'auth0'`** — `agentcore_deployment.py`
   imported `from auth0.authentication import GetToken` but never actually used
   `GetToken` anywhere else in the file, and `auth0-python` was never in
   `requirements.txt` either. Dead import, not a missing dependency — deleted the import
   line rather than adding a new dependency for nothing. (Also removed an adjacent
   duplicate `from dotenv import load_dotenv` / `load_dotenv()` pair at the top of the
   same file while in there — leftover copy-paste, same pattern as the duplicated
   configure+launch blocks noted earlier.)
3. **`ModuleNotFoundError: No module named 'bedrock_agentcore_starter_toolkit'`** — this
   one *is* a real missing dependency (`Runtime` is used throughout the file for
   `configure()`/`launch()`). Added `bedrock-agentcore-starter-toolkit>=0.1.0` to
   `registerAgent/requirements.txt`.

**Preemptive audit after that**, checking every import in `agentcore_agent.py`/
`agentcore_deployment.py` against `requirements.txt` (and vice versa, using PyPI's actual
package metadata rather than guessing):
- `mcp` (imported for `MCPClient`'s transport) isn't listed explicitly, but confirmed via
  `strands-agents`' own PyPI metadata that `mcp>=1.23.0` is one of its core (non-optional)
  dependencies — no gap, no action needed.
- `aiohttp` isn't imported directly anywhere, but confirmed via `openfga-sdk`'s PyPI
  metadata that it depends on `aiohttp>=3.9.3` internally — legitimate explicit pin of a
  real transitive need, left alone.
- **Five packages in `requirements.txt` were dead — never imported anywhere in
  `registerAgent/`**: `langchain`, `langchain-aws`, `langchain-core`, `langgraph` (this
  codebase runs entirely on Strands Agents, no LangChain/LangGraph usage at all — looks
  like leftover boilerplate from a different template), and `strands-agents-tools`
  (`SequentialToolExecutor`/`MCPClient` both ship as part of the *core* `strands-agents`
  package — `strands.tools.executors`/`strands.tools.mcp` — not this separate add-on
  package, which is never imported). **Removed all five** from `requirements.txt`.

## `Runtime.configure()` doesn't take `environment_variables` — my earlier fix was wrong

The env-var-injection approach adopted for dropping AWS Secrets Manager (see above) added
`environment_variables=RUNTIME_ENV_VARS` to `agentcore_runtime.configure(...)`. That
raised `TypeError: Runtime.configure() got an unexpected keyword argument
'environment_variables'` on first real run — I'd flagged at the time that I couldn't
verify this against the installed SDK locally, and it turned out wrong.

Checked the actual toolkit source
(`bedrock_agentcore_starter_toolkit`, `notebook/runtime/bedrock_agentcore.py`) to get it
right: environment variables are a parameter of **`launch()`**, named **`env_vars`** — not
a `configure()` parameter at all.

**Change made**: removed `environment_variables=RUNTIME_ENV_VARS` from both `configure()`
calls; both `launch()` calls now read `agentcore_runtime.launch(env_vars=RUNTIME_ENV_VARS)`
instead of `agentcore_runtime.launch()`. `RUNTIME_ENV_VARS` itself (the dict of ~17 values
read from `.env`) is unchanged — only where it gets passed.

## Pivoted `agentcore_deployment.py`'s local AWS auth from a raw credential triple to an SSO profile

First hit `botocore.exceptions.ClientError: InvalidClientTokenId` running the deploy
script. Confirmed the code genuinely was reading `.env` correctly — the earlier
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` presence check ([agentcore_deployment.py](../registerAgent/agentcore_deployment.py))
was passing, so the failure was AWS itself rejecting the credential *values*, not a code
bug. Root cause, in order of what we found: (1) the values in `registerAgent/.env` were
still the literal placeholder text from `env.sample`, never replaced — fixed by pasting
in a real SSO-derived Access Key/Secret/Session Token triple; (2) `load_dotenv()` used
the default `override=False`, so if the shell already had stale `AWS_*` vars exported
from earlier in the session, `.env`'s values would be silently ignored — fixed by
switching to `load_dotenv(override=True)`.

Both fixes were reasonable, but this script only ever runs locally (it never executes
inside the deployed container — that's `agentcore_agent.py`'s job, under its own IAM
execution role), so hand-copying a rotating, short-lived credential triple every time it
expires is unnecessary friction for something that's always run from the same machine.
Since `aws configure sso` was already set up and returns a working `--profile` name,
pivoted to that instead.

**Change made**: both duplicate credential-check blocks now check `AWS_PROFILE` instead
of `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, and both `boto_session = Session()` calls
became `Session(profile_name=os.getenv('AWS_PROFILE'))`. `env.sample` swapped the
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` block for a single
`AWS_PROFILE=your-sso-profile-name`.

One mechanic worth remembering: the call that actually failed (`InvalidClientTokenId`)
happens deep inside the toolkit's own code via a bare `boto3.client("sts")` — it never
touches our local `boto_session` object at all. What makes that call (and every other
bare `boto3.client(...)` call inside the toolkit) use the right SSO profile is that
`AWS_PROFILE` is a real process environment variable once `load_dotenv(override=True)`
runs — boto3's global default session automatically honors `AWS_PROFILE` from the
environment, with no explicit wiring needed beyond that.

**To use this**: set `AWS_PROFILE=<name>` in `registerAgent/.env` (the name `aws
configure sso` gave you), and separately run `aws sso login --profile <name>` at least
once per SSO session lifetime so a valid cached token exists on disk — boto3 reads that
cache, it doesn't prompt for login itself.

## Removed the duplicated configure+launch block

Flagged a few times earlier (first when figuring out how to run the deployment, again
when adding the ARN print) but never actually removed until now. `agentcore_deployment.py`
had the entire configure+launch sequence twice, back to back — verified line-by-line
that the two blocks were functionally identical before touching anything: repeated
imports (no-op, Python caches modules), `boto_session`/`region` recomputed to the same
values, the second block reusing the first's `RUNTIME_ENV_VARS` unchanged, an
`authorizer_configuration` dict identical except for an inert trailing comma, and both
targeting the same `agent_name="agentcore_agent_a4aa"` with the same `entrypoint`. Running
the script configured and deployed the identical agent twice in a row — same end state,
just double the deploy time.

**Change made**: deleted the second block entirely. One configure+launch sequence now.

## Root `requirements.txt` had two real conflicts and a missing dependency, once `deployAgentCore` finally worked and `runLocalApp` got tried for real

Same style of audit as `registerAgent/requirements.txt` earlier, applied to the root one,
triggered by two `pip` `ResolutionImpossible` errors in a row plus one thing spotted
while auditing.

1. **`fastapi==0.111.0` conflicted with `auth0-fastapi==1.0.0b5`**, which requires
   `fastapi>=0.115.11,<0.117.0`. Bumped to `fastapi==0.116.2` — newest release inside that
   range.
2. **`auth0-python==4.7.1` conflicted with `auth0-server-python==1.0.0b6`** on
   `cryptography`: `auth0-python` requires `cryptography<43.0.0`, `auth0-server-python`
   requires `cryptography>=43.0.1` — mutually exclusive, no version satisfies both.
   Checked whether `auth0-python` was actually needed at all first: `app.py:13` imported
   `GetToken` from it but never called it anywhere — identical dead-import pattern to the
   one already found and removed in `registerAgent/agentcore_deployment.py`. Confirmed via
   PyPI metadata that neither `auth0-fastapi` nor `auth0-server-python` depend on
   `auth0-python` either. **Removed the import from `app.py` and dropped
   `auth0-python==4.7.1` from `requirements.txt` entirely** — the cleanest fix, since
   nothing actually needs the package.
3. **`boto3` was missing from `requirements.txt` entirely**, despite `app.py` importing
   and using it extensively (the DynamoDB session store, `app.py:56-61` etc.) — it only
   ever worked before by accident, if boto3 happened to already be present globally.
   Added `boto3>=1.34.0` (same version floor used in `registerAgent/requirements.txt`).

**Confirmed legitimate, left alone**: `Authlib` (not imported directly, but a real
dependency of `auth0-server-python`, which requires `authlib<2.0,>=1.2` — checked via
PyPI metadata), `uvicorn[standard]` (runs the app externally via `uvicorn app:app`, never
imported in source), `Jinja2` (used via `fastapi.templating.Jinja2Templates`, not
imported by name directly), and `auth0-server-python` itself (a real transitive
dependency of `auth0-fastapi>=1.0.0b6`, which the pinned `auth0-server-python==1.0.0b6`
satisfies exactly).

## Two more missing dependencies — both Starlette/FastAPI "optional extras" that fail at call-time, not import-time

`runLocalApp` got past `pip install` cleanly (previous fixes worked) but crashed at
startup: `ModuleNotFoundError: No module named 'itsdangerous'`, raised from inside
`starlette/middleware/sessions.py` when `app.py:19`'s `from starlette.middleware.sessions
import SessionMiddleware` executes. `itsdangerous` signs the session cookie — Starlette
treats it as an optional extra, so it's never pulled in as a hard dependency of
`starlette`/`fastapi` even though `app.py` uses `SessionMiddleware` directly
(`app.py:82`).

Preemptively checked for the same pattern elsewhere before it caused a second
round-trip: `app.py:460` calls `await request.form()`, which needs `python-multipart` —
same "optional extra, fails at call-time not import-time" shape, would have surfaced
later as a runtime error the moment that route was hit rather than at startup.

**Change made**: added `itsdangerous==2.2.0` and `python-multipart==0.0.32` to
`requirements.txt`.

## `AUTH0_CONNECTION_NAME` missing from `env.template` — breaks "Connect Account"

Clicking "Connect Account" failed with a pydantic `ValidationError`:
`ConnectAccountOptions.connection` got `None` instead of a string. Traced to
`app.py:53` (`AUTH0_CONNECTION_NAME = os.getenv("AUTH0_CONNECTION_NAME")`) — unset,
because it's genuinely missing from `env.template` entirely, same category of gap as the
earlier missing `SESSION_TABLE_NAME`. Confirmed it's a real, used value, not dead — three
call sites: `app.py:379` (the one that broke), `:426`, `:618`.

This is the enterprise (Okta OIDC) connection set up right at the start of this doc's
"Creating an enterprise connection" section — the value needed here is that connection's
own **Name** field (Auth0 Dashboard → Authentication → Enterprise → the connection →
Name), not the Okta domain or any display label.

**Change made**: added `AUTH0_CONNECTION_NAME` to `env.template` under Auth0
Configuration, with a comment pointing at where to find the right value.

## "Connect Account" fails with `MyAccountApiError: Invalid Token` — missing scope

Traced by pulling the *exact* pinned `auth0-server-python==1.0.0b6` wheel's source
(matched the traceback's line numbers exactly, rather than the newer code on GitHub
main). `start_connect_account` does its own token exchange for the MyAccount API's
audience (`https://{domain}/me/`) with `scope="create:me:connected_accounts"`, merged
with whatever's in `AUTH0_SCOPE` — it does **not** reuse the session's
`AUTH0_AUDIENCE`-scoped token. `AUTH0_SCOPE` only had `read:me:connected_accounts`,
never `create:me:connected_accounts` — since that scope was never part of the original
login consent, the refresh-token exchange for it comes back without it, and the MyAccount
API rejects the resulting token for a `connect` (write) operation.

**Change made**: added `create:me:connected_accounts` to `AUTH0_SCOPE` in `app.py`.
Requires logging out and back in — the fix only takes effect on a fresh `/authorize`
round trip, not the current session's already-issued tokens.

## Full hardcoded-values sweep of the web app, prompted by the scope bug above

Auditing the code right around the scope bug turned up several more hardcoded values
that should be `.env`-driven, in both `app.py` and the unused `connect_account.py`
(fixed in both for consistency, even though the latter isn't actually served by
`runLocalApp`):

- **`MYACCOUNT_BASE_URL = "https://smalser5.eu.auth0.com"`** — hardcoded to what looks
  like the *original repo author's own Auth0 tenant domain*, in both files. Actually used
  (`app.py:146`, fetching connected accounts) — meaning the app was calling a different
  tenant's MyAccount API, not yours. The MyAccount API's base URL is always
  `https://{your-tenant-domain}/me/` (confirmed from the SDK's own
  `MyAccountClient.audience` property while tracing the scope bug above), fully derivable
  from `AUTH0_DOMAIN`, which is already `.env`-driven. **Change made**: now
  `MYACCOUNT_BASE_URL = AUTH0_BASE_URL` (derived, no new env var needed) in both files.
- **`AUTH0_SCOPE`** was a plain hardcoded literal in `app.py`, not read from `.env` at
  all (this is the dead-env-var finding from earlier, now reversed). **Change made**: now
  `os.getenv("AUTH0_SCOPE", "<same default, including create:me:connected_accounts>")`.
  Also fixed the stale `env.template` value — it still said
  `AUTH0_SCOPE=invoke:gateway read:gateway` (harmless while dead, but wrong and now live) —
  updated it to the real default, with a comment warning not to drop
  `create:me:connected_accounts` if customizing it.
- **`CONNECTED_ACCOUNT_SCOPE`** had the `os.getenv(key, default)` bug noted earlier
  (calling `os.getenv` with the scope string itself as the *key*, e.g.
  `os.getenv("myaccount:manage_connections", "openid profile...")` — always falls through
  to the default since that key never exists as an env var). **Change made**: now
  `os.getenv("CONNECTED_ACCOUNT_SCOPE", "<same default value as before>")` — preserves
  today's exact runtime behavior while actually making it overridable. Added
  `CONNECTED_ACCOUNT_SCOPE` to `env.template` too.
- Also removed one redundant duplicate assignment in `app.py` (`AUTH0_BASE_URL` and
  `AUTH0_SECRET` were each assigned twice in a row, same pattern as other duplication
  found earlier in this doc) while in the area.

## "Connect Account" still fails after the scope fix — the real cause was a missing Multi-Resource Refresh Token (MRRT) policy

The `create:me:connected_accounts` scope fix (above) didn't actually resolve
`MyAccountApiError: Invalid Token` — the Auth0 tenant logs made the real cause visible.
The refresh-token exchange requested the right audience (`https://{tenant}/me/`) and
scope (including `create:me:connected_accounts`), but Auth0 silently **ignored the
requested audience entirely** and re-issued a token for the original
`https://agentcore.summit` audience instead, with a scope that dropped
`create:me:connected_accounts` (and other scopes) — that mismatched token is what the
MyAccount API then correctly rejected.

**Why**: by design, a refresh token can only be exchanged for an access token scoped to
a *different* audience than the one it was originally issued under if that audience is
explicitly allow-listed via a **Multi-Resource Refresh Token (MRRT) policy** on the
Auth0 Application. Undocumented-until-you-hit-it behavior: if no MRRT policy exists for
the requested audience, Auth0 doesn't error — it silently falls back to the original
audience/scope (per
[Auth0's own MRRT docs](https://auth0.com/docs/secure/tokens/refresh-tokens/multi-resource-refresh-token/configure-and-implement-multi-resource-refresh-token)).
Nothing about needing this was ever mentioned anywhere in the blog.

**This is Management API-only — no Dashboard toggle exists** (confirmed by checking the
docs specifically for this). Two ways to set it:
- Raw Management API: `PATCH /api/v2/clients/{client_id}` with a `refresh_token` object.
- Auth0 CLI: `auth0 apps update <client_id> --refresh-token '<json>'` — same underlying
  call, just skips manually grabbing a Management API token.

**Gotcha hit along the way**: `refresh_token` is patched as a whole object, not merged —
omitting `rotation_type`/`expiration_type` (both required) fails with a 400, even though
you're only trying to add a `policies` array. Fetch the current full `refresh_token`
block first (`auth0 apps show <client_id> --json`) and include its existing
`rotation_type`/`expiration_type`/etc. alongside the new `policies` entry, rather than
sending `policies` alone.

**End state that worked** — `refresh_token.policies` on the "AgentCore - Web app" client
now has two entries: one for the MyAccount API audience
(`https://{tenant}/me/`, scoped to `create:me:connected_accounts`,
`read:me:connected_accounts`, plus the other MyAccount scopes this app touches for
authentication-method/CIBA enrollment), and one for `https://agentcore.summit` itself,
scoped to just `invoke:gateway`/`read:gateway`.

**Worth watching later**: that second policy narrows what scope a *refreshed* (not
freshly-logged-in) token for `agentcore.summit` can carry to just those two gateway
scopes — `okta.users.read` etc. won't be present on a refreshed token for that audience.
Not an issue today since the current session's token came from the initial login's
authorization-code exchange, not a refresh, but worth remembering if `getOktaGroups` or
similar starts failing specifically after a token refresh later on.

Confirmed via the tenant's own auth logs (`type: "sertft"`, "Successful Refresh Token
exchange") that after applying the MRRT policy, `details.policy_used` changed from
absent/default to `"mrrt"`, and the granted `audience`/`scope` finally matched what was
requested.

## Connection-level `connected_accounts` toggle also required — separate from Application config

The next block after MRRT: `MyAccountApiError: The specified connection does not
support connected accounts or is not active`. This is a **connection-level** setting,
distinct from everything configured so far (which was all Application-level): under the
Okta connection itself, Auth0 Dashboard → Authentication → Enterprise Connections → your
connection → **Purpose** section → toggle on **Connected Accounts for Token Vault**
(Kevin enabled it as "Authentication and Connected Accounts for Token Vault"). Also
worth checking while there: the connection's **Applications** tab has the web app
toggled on.

## Okta-side redirect_uri also needed registering — same `/login/callback` pattern from Part 1

Next: Okta rejected the request outright — `'redirect_uri' parameter must be a Login
redirect URI in the client app settings`. This is the exact same Auth0 IdP callback
pattern established when the enterprise connection was first created
(`https://{AUTH0_DOMAIN}/login/callback`) — just needed adding to the Okta OIDC app's
own **Sign-in redirect URIs** list (Okta Admin Console → the specific app → General).
Confirms the connection itself was fine; this was purely an Okta-app-side gap.

## `CONNECTED_ACCOUNT_SCOPE` including `okta.users.read` broke the connection login — confirmed

After the above two fixes, hit `Something went wrong` / `Custom scopes are not allowed
for this request` on Auth0's own `/login/callback`, despite a Auth0 tenant log showing
"Successful Refresh Token exchange" for the *unrelated* MRRT/MyAccount step — this error
was from a *different* leg (the actual Okta connection login), not the MyAccount API
call.

Traced `CONNECTED_ACCOUNT_SCOPE` (`app.py:379-386`) as the direct, unmodified source of
the `scopes` sent to Okta for that connection login — confirmed via the pinned SDK
source that it flows straight through `ConnectAccountOptions.scopes` →
`ConnectAccountRequest.scopes` in the API request body, and per Auth0's own docs, that's
what gets forwarded to the external IdP as the requested scope. Its default value
included `okta.users.read` — a genuinely non-standard scope (Okta's Org Authorization
Server scope for calling Okta's own Users/Groups API directly), which doesn't belong in
a connection-login scope request at all; that's a separate concern handled entirely by
Token Vault inside `getOktaGroups`.

**Change made, confirmed working**: stripped `CONNECTED_ACCOUNT_SCOPE`'s default down to
`"openid profile email offline_access"` in `app.py` and `env.template`
(`connect_account.py` already had this correct value from an earlier pass). Kevin
confirmed this resolved the "Custom scopes" error — no logout/login needed, since this
scope is read fresh on every "Connect Account" click, not baked into a token at login.

## Final blocker — Okta app missing the Refresh Token grant type. Confirmed fixed.

Last error in this chain: `Missing refresh token. Connected accounts requires offline
access to function properly.` Since `offline_access` was already present in
`CONNECTED_ACCOUNT_SCOPE` (confirmed above), this pointed at the other cause the error
names: the identity provider not configured to actually *issue* a refresh token — the
exact requirement flagged all the way back when this connection was first set up
("Must enable the Refresh Token grant type / offline_access scope on the Okta app").

**Root cause, confirmed by Kevin**: the Okta OIDC app (Okta Admin Console → the app →
General → Grant type) never actually had **Refresh Token** checked, despite
`offline_access` being requested at the scope level — requesting the scope isn't
sufficient on its own; the app has to be explicitly allowed to issue one. Enabling that
grant type on the Okta app resolved it.

**End-to-end result**: full "Connect Account" flow now works — Auth0 → My Account API →
Okta OIDC login → back to Auth0 → connected account shows as linked. This closes out the
entire chain of issues starting from the original `ConnectAccountOptions.connection`
validation error: connection name → CIBA/AUTH0 var consolidation → MRRT policy →
connection Purpose toggle → Okta redirect_uri → connection scope → Okta Refresh Token
grant type. Six distinct, separately-gated prerequisites, none of them documented
together anywhere in the blog.

## One more spot with the same wrong-audience bug: `fetch_federated_tokens`

Even with "Connect Account" fully working, the web app terminal logged
`401 Client Error: Unauthorized` for `.../me/v1/connected-accounts/accounts`. Same root
cause as the `start_connect_account` fix earlier in this doc, different call site:
`fetch_federated_tokens` (`app.py:131-158`) sent the session's general `access_token`
(audienced to `https://agentcore.summit`) straight to the MyAccount API's "list
connected accounts" endpoint, instead of getting a token actually audienced for
`https://{domain}/me/`. The SDK's own `start_connect_account` does this correctly
internally (`get_access_token(audience=..., scope=...)`); this hand-rolled call site
never did.

**Change made**: `fetch_federated_tokens` now calls
`auth_client.client.get_access_token(audience=f"{MYACCOUNT_BASE_URL}/me/",
scope="read:me:connected_accounts", ...)` first and uses *that* token for the MyAccount
API call, same pattern the SDK uses internally. `read:me:connected_accounts` was already
in the MRRT policy configured earlier, so no further Auth0-side change needed.

## Chat still 401s after session/DynamoDB region fix — `allowedClients` checks a claim Auth0 tokens don't have

Once DynamoDB session persistence was actually working (the table had been created in the
wrong region — separate, self-diagnosed fix: make sure the table's actual region matches
`AWS_REGION`), sending a chat message still 401'd invoking the AgentCore Runtime.

Decoded the session's actual bearer token to inspect it directly (Starlette's
`SessionMiddleware` cookie is signed but not encrypted — payload is the base64 segment
before the first `.` in the cookie value). Confirmed `exp` was fine (not expired) and
`azp` matched the expected client. Ruled out the invocation URL construction
(`app.py:509`, `f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com/runtimes/{arn}/invocations?qualifier=DEFAULT"`)
by comparing byte-for-byte against AWS's own official sample code for this exact
OAuth-invoke pattern — identical.

**Root cause, confirmed by AWS's own docs and then by the actual error body**: AWS's
`customJWTAuthorizer` validates `allowedClients` against a literal **`client_id`** claim
in the token. Auth0 access tokens never include a `client_id` claim — they use `azp`
instead. Since `registerAgent/agentcore_deployment.py`'s authorizer config set
`allowedClients`, that check could never pass, no matter what value was configured.
Confirmed directly once the web app's swallowed error body got surfaced (see below):
`{"error":{"code":-32001,"message":"Claim 'client_id' value mismatch with configuration."}}`.

Also ruled out along the way: the token's `aud` claim is an array (Auth0 automatically
adds a second `/userinfo` audience whenever you request a custom `audience` alongside
typical OIDC scopes) — confirmed via AWS's docs this is handled correctly ("one of the
values in `aud`... should match"), not a bug.

**Change made**: removed `allowedClients` entirely from the `customJWTAuthorizer` config
in `registerAgent/agentcore_deployment.py` — `allowedAudience` alone is the actual
security boundary here, since `allowedClients` can never be satisfied by an Auth0 token
regardless of value. **Requires a redeploy** (`./deployAgentCore`) since this is baked
into the Runtime at deploy time.

**Also fixed while debugging this**: `app.py`'s `except requests.exceptions.HTTPError`
handler around the invoke call only logged `str(exc)` (just the HTTP status line) —
never the actual response body, which is where AWS puts the specific rejection reason.
Now logs `exc.response.text` too. This is what actually surfaced the exact error message
above instead of a bare "401 Unauthorized" — worth keeping this pattern in mind
elsewhere: `raise_for_status()` errors need the response body logged separately, it's
not included in the exception's own string representation.

**Also confirmed**: the local toolkit cache `registerAgent/.bedrock_agentcore.yaml` (not
committed, machine-local) fully overwrites `authorizer_configuration` on each
`configure()` call rather than merging — the stale `allowedClients` entry we saw there
briefly was just because the redeploy hadn't been re-run yet after the code change, not
a merge-behavior bug. No special cache-clearing needed between config changes; a normal
`./deployAgentCore` re-run is sufficient.

## Next 401 gone, now a 400 — `runtimeSessionId` too short

Auth now passes. New error:
`Value at 'runtimeSessionId' failed to satisfy constraint: Member must have length
greater than or equal to 33`. `app.py` was sending the Auth0 `user_id` claim
(`"auth0|" + 24 hex chars` = 30 characters) — or the 16-character `"default-session"`
fallback — as the `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header
(`app.py:511-517`). Both under AWS's 33-character minimum for `runtimeSessionId`.

**Change made**: reused the function's own `session_id` (a UUID4, 36 characters,
already validated non-empty a few lines above for the DynamoDB lookup) instead of a
separate, too-short value. Also removed a temporary debug `print(bearer_token)` added
earlier in this session for token decoding — no longer needed now that the response body
is logged properly.

## First real 500 from inside the deployed container — three separate things bundled in one log

Auth and payload validation both passed this time — the request actually reached the
container, which then errored. Found the real CloudWatch log group for this
(`/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint_name>`, standard stdout/stderr
under a `[runtime-logs] <uuid>` stream, OTEL structured logs under `otel-rt-logs` — per
[AWS's observability docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-view.html)).
Earlier attempts to tail this showed nothing simply because no request had reached the
container yet — every prior failure was rejected upstream at the authorizer/validation
layer, before the container ever started. Three distinct things showed up in one trace:

1. **Real IAM gap — execution role missing DynamoDB permissions.**
   `AccessDeniedException` on `dynamodb:GetItem` against the `bedrock-sessions` table,
   from the Runtime's auto-created execution role
   (`AmazonBedrockAgentCoreSDKRuntime-us-west-2-2d87e428f8`). `auto_create_execution_role=True`
   creates a role but doesn't attach any DynamoDB access — that has to be added manually.
   This alone doesn't crash the request (it's caught in `agentcore_agent.py`'s own
   try/except around the DynamoDB lookup), but leaves `access_token` empty, which breaks
   anything downstream that needs it (the MCP Gateway call's own Authorization header).
   **Needs a manual IAM policy attachment** — not a code or redeploy fix:
   ```json
   {
     "Effect": "Allow",
     "Action": ["dynamodb:GetItem", "dynamodb:PutItem"],
     "Resource": "arn:aws:dynamodb:us-west-2:581334898892:table/bedrock-sessions"
   }
   ```
2. **Expected, not a bug — MCP Gateway not set up yet.** `httpx.ConnectError: Name or
   service not known` trying to resolve `MCP_GATEWAY_URL` — DNS failure, because the
   AgentCore Gateway itself hasn't been created in this lab yet (a later, not-yet-reached
   part of the blog). Nothing to fix here directly; just expected until the Gateway
   exists.
3. **Real code bug — the actual cause of the 500.** `UnboundLocalError: cannot access
   local variable 'agent'`. The `except` block around the failed `with MCPClient(...) as
   mcp_client:` tried a "fallback: answer with local tools" by calling
   `resp = agent(user_input)` — but `agent` is only ever assigned *inside* the `with`
   block (after `Agent(...)` is constructed), which never ran because the connection
   failure (#2) happened during `__enter__`, before the block's body executed. So the
   fallback never worked — it just crashed differently. There was also unreachable dead
   code after the whole `try/except` (`resp = agent(user_input)` again, post-return) —
   another leftover fragment, same pattern as other duplicated code found earlier in this
   doc.

   **Change made**: the `except` block now actually builds a fallback `Agent` with just
   the local tools (`weather`, `getOktaGroups`, `invokeCiba`, no `remote_tools`) before
   invoking it — so a Gateway-unavailable request degrades gracefully instead of
   crashing. Pulled `system_prompt` out to a shared local variable so both the
   success-path and fallback-path `Agent(...)` constructions stay in sync. Removed the
   unreachable dead code after the try/except.

**Still needed for full functionality (not yet done)**: the IAM policy in #1, and
eventually the actual Gateway setup for #2 — remote/Gateway-backed tools won't work
until both are in place, but local tools (chat, CIBA, Okta groups) should now work with
just the IAM fix.

## Hardcoded Bedrock model ID had reached end-of-life

After the fallback fix above, hit a new, different error:
`botocore.errorfactory.ResourceNotFoundException: ... This model version has reached the
end of its life` for `us.anthropic.claude-3-7-sonnet-20250219-v1:0`
(`agentcore_agent.py:345`, hardcoded). Model availability/deprecation on Bedrock changes
over time independent of anything in this repo.

**Change made**: `model_id = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-3-7-sonnet-20250219-v1:0")`
— added to `registerAgent/env.sample` and `agentcore_deployment.py`'s pushed
`RUNTIME_ENV_VARS` list. **Not yet resolved** — didn't guess a replacement model ID
without verifying it's actually available in this account/region (that's exactly how the
original one broke). Check the Bedrock console's model catalog for a currently-available
Claude inference profile ID and set `BEDROCK_MODEL_ID` to that.

**Gotcha**: `RUNTIME_ENV_VARS` uses `os.getenv(key, "")` — an *empty* value for a var
still counts as "set" when it reaches `agentcore_agent.py`'s own `os.getenv(key,
default)` call, so leaving `BEDROCK_MODEL_ID` blank in `.env` doesn't fall through to the
hardcoded default — it pushes an empty string and fails differently. Must be a real,
non-empty value.

**Resolved**: found via `aws bedrock list-inference-profiles` run directly (in Kevin's
own terminal, not mine — no AWS creds in this session's shell) —
`us.anthropic.claude-haiku-4-5-20251001-v1:0` is live and working in this account/region.
Also confirms the account had already burned through two more deprecated guesses
(`claude-3-5-haiku-20241022` also end-of-life) before landing on this — querying the API
directly for what's actually live beat guessing from documentation/training knowledge.

## FGA relation name mismatch — code says `read_groups`, blog's actual model says `read_okta`

Same "code and blog drifted apart" pattern as everything else in this doc. Model got
past Bedrock and successfully called the `getOktaGroups` tool, which then failed the FGA
check: `HTTP 400 object relation does not exist`. `agentcore_agent.py:274` hardcoded
`"relation": "read_groups"`, but the blog's actual authorization model (confirmed by
Kevin, who has it from the blog) defines the relation as **`read_okta`** on type `okta`:
```
type okta
  relations
    define read_okta: [user, group#member]
```

**Change made**: `agentcore_agent.py:274` now sends `"relation": "read_okta"` to match.

**Still needed, not yet confirmed done**: an actual FGA tuple granting the relation —
the code checks object `okta:groups` (type `okta`, id `groups`) specifically, so there
needs to be a tuple like `user:kevin.akermanis@okta.com` → `read_okta` → `okta:groups`
in the FGA store, or the check will now be schema-valid but still return
not-authorized for lack of a matching tuple.

**Confirmed working**: FGA check now returns `{'allowed': True}`, and the DynamoDB IAM
policy fix (above) is confirmed working too (no more `AccessDeniedException` in later
traces) — both resolved.

## `OKTA_DOMAIN` never got the scheme-stripping treatment applied to its siblings

Next: `getOktaGroups` failed with `HTTPSConnectionPool(host='https')` — a malformed URL.
`CIBA_AUTH0_DOMAIN` and `FGA_API_URL` both already strip `https://`/`http://` prefixes
defensively (`agentcore_agent.py`), but `OKTA_DOMAIN` never got the same treatment. Since
`OKTA_DOMAIN` was set to a full URL (`https://demo-peach-salmon-30608-admin.okta.com`)
rather than a bare domain, `f'https://{OKTA_DOMAIN}/...'` doubled the scheme, and the
resulting malformed URL got misparsed as host=`https`.

**Change made**: `OKTA_DOMAIN` now strips `https://`/`http://`/trailing `/` the same way,
matching the existing pattern for the other two domain-shaped env vars in this file.

**Also wrong, separate from the scheme bug**: the `.env` value itself was
`demo-peach-salmon-30608-**admin**.okta.com`. Okta orgs expose two separate hostnames —
the base org domain (`demo-peach-salmon-30608.okta.com`), used for both the end-user
experience *and* the `/api/v1/...` Users/Groups API, and the `-admin` hostname, which is
strictly for the Admin Console UI. Hitting `/api/v1/users/...` against the `-admin` host
returns a `403` with an **empty body** — this is Okta's edge blocking an unsupported path,
not an application-level rejection (a real Okta API 403 comes back with a JSON body like
`{"errorCode":"E0000006","errorSummary":"..."}`; an empty body on 403 is the tell).

**Fix**: removed `-admin` from `OKTA_DOMAIN` in `.env`, redeployed.

## MCP Gateway: `Name or service not known` → `403 Forbidden`

Separately, `MCP_GATEWAY_URL` in `agentcore_deployment.py`'s `RUNTIME_ENV_VARS` wasn't
actually being set, so the MCP client failed DNS resolution entirely
(`httpx.ConnectError: [Errno -2] Name or service not known`). Fixed the env var wiring,
redeployed — the client now resolves the Gateway host but gets a `403 Forbidden` back
from `https://sesummitmockgateway-*.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp`.

This is a distinct, **not-yet-investigated** gap — Gateway inbound auth/setup was already
a known-deferred item (remote/MCP tools aren't required for the demo's main path; the
agent falls back gracefully to local-only tools per the `except` block in
`strands_agent_bedrock`). Revisit if remote tools become a demo requirement.

## Root cause of the persistent Okta 403: federated token lacks `okta.users.read` scope

After both fixes above, `getOktaGroups` still 403s — but now with a body-less response
carrying a `WWW-Authenticate` header instead (added temporary logging of
`user_response.headers` at `agentcore_agent.py:325` to catch this):

```
www-authenticate: Bearer authorization_uri="http://demo-peach-salmon-30608.okta.com/oauth2/v1/authorize",
  realm="http://demo-peach-salmon-30608.okta.com", scope="okta.users.read",
  error="insufficient_scope",
  error_description="The access token provided does not contain the required scopes.",
  resource="/api/v1/users"
```

This confirms it precisely: the federated/Connected-Accounts access token stored in
DynamoDB (`federated_token`) is valid and reaches Okta fine, but it was never issued with
the `okta.users.read` scope needed to call the Users/Groups Management API. This traces
back to a wall hit earlier in the build (see [[agentcore-auth0-platform-gotchas]]):
requesting `okta.users.read` via `ConnectAccountOptions.scopes` in `app.py` gets rejected
by Auth0's own `/login/callback` with *"Custom scopes are not allowed for this request"* —
so the scope was never added to what Connected Accounts requests from Okta on login.

**Status: unresolved.** This is an Auth0 Connected Accounts platform constraint, not a
code bug in this repo. Possible paths to resolution, not yet attempted:

1. Look for an Auth0-side mechanism to allow-list a non-standard external-IdP API scope
   for Connected Accounts, separate from the `ConnectAccountOptions.scopes` list that
   triggers the rejection — e.g. a connection-level scope configuration, or a supported
   pattern for passing provider-specific (non-OIDC-standard) scopes through Token Vault.

   **Research done (2026-08-25 evening) into two candidate mechanisms found via
   search — verified against the installed SDK source, not yet against Okta live**:

   - **(a) Dynamic `connection_scope`/`requested_connection_scope` param — does NOT
     apply to this repo's flow.** Traced this fully through the installed
     `auth0_server_python`/`auth0_fastapi` SDK source
     (`.venv/lib/python3.12/site-packages/auth0_server_python/auth_server/server_client.py`).
     `requested_connection_scope` is a real Auth0 mechanism, but it belongs to the
     **classic Account Linking flow** (`_build_link_user_url`, `StartLinkUserOptions`) —
     a browser redirect straight to Auth0's `/authorize` endpoint with
     `requested_connection`/`requested_connection_scope` query params. This repo's
     `connect_account_start` in `app.py` calls `auth_client.start_connect_account(...)`
     instead, which is a **completely different Auth0 feature**: the **Connected
     Accounts (My Account API)** flow. That method builds a `ConnectAccountRequest`
     (with a `scopes: list[str]` field, from `CONNECTED_ACCOUNT_SCOPE`) and does an
     authenticated `POST {domain}/me/v1/connected-accounts/connect`
     (`auth0_server_python/auth_server/my_account_client.py:24`) — there is no
     `/authorize` redirect step in this path for a `connection_scope` query param to
     attach to. **This lead doesn't transplant as-is** — it's for a flow this repo
     doesn't use.
   - **(b) Static `upstream_params` on the connection via Management API — more
     promising, not yet tried.** This is a genuinely separate, connection-level Auth0
     feature (`PATCH /api/v2/connections/{id}` with `options.upstream_params`) that
     controls what extra params Auth0 forwards to the upstream IdP (Okta) on login,
     independent of whatever the client app requests. Since it's connection-config
     rather than app-code, this wouldn't need any `app.py` changes — just a Management
     API call against the Okta connection. **Not yet verified against Auth0's actual
     Connections API docs/schema** (need to confirm `upstream_params` supports
     injecting an OAuth2/OIDC `scope` value for an Okta enterprise connection
     specifically, and that it actually reaches the token that Connected Accounts later
     fetches via Token Vault) — this is the next thing to check, before writing any code.
2. Sidestep entirely: have `getOktaGroups` call Okta's API with an **Okta API token
   (SSWS)** configured server-side, instead of the end-user's federated OAuth token. This
   would work immediately, but changes what the demo actually proves — it stops being "the
   agent calls Okta using the user's own delegated permissions" and becomes "the agent has
   its own standing Okta credential," which is a materially different (and less
   interesting) security story for the lab.
3. Leave as a documented limitation. Since this is a from-scratch reproduction of the
   Auth0 blog, an honest note that "the tutorial's Connected Accounts approach hits an
   Auth0 platform scope restriction for non-standard external-IdP scopes" may itself be
   useful content for other SEs attempting this lab, rather than something to route around.

Next session: decide between options 1–3 above before continuing the rest of the demo
flow (CIBA password-reset, FGA tuple-removal-revokes-access).

## RESOLVED (2026-08-26): the fix was CONNECTED_ACCOUNT_SCOPE + the connection already having the scope

Backed up the Okta connection's live config first (`oktaConnectionConfBackup.json`,
`auth0 api get connections/con_hPU85mxrqQPtAfnn`) before touching anything. That backup
revealed the connection's own `options.scope` field **already included `okta.users.read`**:
```
openid profile email offline_access okta.users.read
```
This is a documented, standard field for generic OIDC connections (`strategy: "oidc"`) —
the scope Auth0 requests from the upstream IdP by default — and it's a *different*
mechanism from `upstream_params` (which was the leading candidate going into this
session; turned out to be unnecessary here since the simpler, more direct field was
already correctly set).

Despite that, a live check of the connected account's granted permissions (in the Auth0
dashboard) showed only `email offline_access openID profile` — no `okta.users.read`. Root
cause, confirmed by re-reading `app.py`: **`connect_account_start` explicitly overrides
the connection's default scope on every call**, sending whatever `CONNECTED_ACCOUNT_SCOPE`
says as the client-requested `scopes` field in the My Account API's
`POST /me/v1/connected-accounts/connect` body (`app.py:386-392`). `CONNECTED_ACCOUNT_SCOPE`
was `openid profile email offline_access` — no `okta.users.read` — so every Connect
Account run (including a fresh re-auth) kept requesting the narrower set, regardless of
what the connection itself was configured to allow.

**Fix applied**: added `okta.users.read` to `CONNECTED_ACCOUNT_SCOPE` in the root `.env`,
then re-ran Connect Account (logout → login → `/connect-account/start`, forcing Okta
re-auth). This time it was accepted — no "Custom scopes are not allowed" rejection.

**Reconciling this with the earlier rejection** (see above): the most consistent
explanation across both observations is that the My Account API's `/connect` endpoint
validates the client-requested `scopes` list against what the **connection's own
configured scope** allows — i.e. it's not that `okta.users.read` can never be
client-requested, it's that it can only be client-requested if the connection is *already*
configured to request it by default. Earlier in the build, the connection didn't have
`okta.users.read` in its `options.scope` yet, so requesting it from the app was rejected;
by the time this was retried, the connection already had it set (unclear exactly when/how
that got added — worth a quick check of Auth0 Logs/audit trail if it matters later), so
the same client-side request succeeded. **Confirmed, not just inferred**: the connected
account's granted-permissions view now shows `okta.users.read`, and `getOktaGroups`
returns the actual group list successfully.

**Practical takeaway for the SETUP.md instructions**: both pieces need to be true —
(1) the connection's own `options.scope` must include `okta.users.read` (Management API
only, no Dashboard UI for this field on an OIDC connection), AND (2) `CONNECTED_ACCOUNT_SCOPE`
in the root `.env` must also include `okta.users.read`. Missing either one produces a
different failure (missing on the connection → rejected at request time; missing from
`CONNECTED_ACCOUNT_SCOPE` alone → request succeeds but the resulting token still lacks the
scope, silently, only surfacing later as an Okta `insufficient_scope` 403).

An unrelated red herring hit during this same testing session: a local `botocore`
`TokenRetrievalError`/`InvalidGrantException` from an expired AWS SSO session, surfaced
inside `store_session_data`'s DynamoDB `put_item` call during `/connect-account/callback`.
This is unrelated to Auth0/Okta scopes — it only blocks the web app's own session
persistence step, not the Connect Account exchange itself (which succeeds before that
point). Fix: `aws sso login --profile <profile>` to refresh the local SSO session.

**End-to-end demo status as of this fix**: login → Connect Account (Okta) → chat →
"what Okta groups am I part of?" now works fully, returning the real group list. CIBA
password-reset and FGA tuple-removal-revokes-access are still untested end-to-end — next
session's actual next step.
