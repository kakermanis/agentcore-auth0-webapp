import os
import json
import time
import urllib.parse
import uuid
import logging
from functools import wraps
from typing import Any, Dict, Optional, Tuple

import boto3
import requests
from dotenv import load_dotenv
from auth0_fastapi.auth import AuthClient
from auth0_fastapi.config import Auth0Config
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware


load_dotenv()

APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "your-secret-key-here")

AUTH0_CLIENT_ID = os.getenv("AUTH0_CLIENT_ID")
AUTH0_CLIENT_SECRET = os.getenv("AUTH0_CLIENT_SECRET")
AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN")
AUTH0_BASE_URL = f"https://{AUTH0_DOMAIN}"
AUTH0_AUDIENCE = os.getenv("AUTH0_AUDIENCE")
SESSION_TABLE_NAME = os.getenv("SESSION_TABLE_NAME")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AUTH0_SECRET = os.getenv("AUTH0_SECRET", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
 
AUTH0_SCOPE = os.getenv(
    "AUTH0_SCOPE",
    "openid profile email offline_access okta.users.read read:me:connected_accounts create:me:connected_accounts",
)

CONNECTED_ACCOUNT_SCOPE = os.getenv(
    "CONNECTED_ACCOUNT_SCOPE",
    "openid profile email offline_access okta.users.read"
)

MYACCOUNT_BASE_URL = AUTH0_BASE_URL
AUTH0_AUTH_PARAMS = {
    "scope": AUTH0_SCOPE,
    "audience": AUTH0_AUDIENCE,
    "prompt": "consent",
    "access_type": "offline",
}
AUTH0_CONNECTION_NAME = os.getenv("AUTH0_CONNECTION_NAME")


dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)

AGENT_RUNTIME_ARN = os.getenv("AGENT_RUNTIME_ARN")

auth_config = Auth0Config(
    domain=AUTH0_DOMAIN,
    client_id=AUTH0_CLIENT_ID,
    client_secret=AUTH0_CLIENT_SECRET,
    secret=AUTH0_SECRET or APP_SECRET_KEY,
    app_base_url=APP_BASE_URL or AUTH0_BASE_URL,
    audience=AUTH0_AUDIENCE,
    callback_path="/auth/callback",
    connect_account_callback_path="/connect-account/callback",
    authorization_params=AUTH0_AUTH_PARAMS,
    scopes=AUTH0_SCOPE,
)

auth_client = AuthClient(auth_config)

app = FastAPI(title="AWS AgentCore Auth0 Chat")
app.add_middleware(SessionMiddleware, secret_key=APP_SECRET_KEY, same_site="lax")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

REFRESH_TOKEN_CACHE: Dict[str, str] = {}


def _store_options(request: Request, response: Response) -> Dict[str, Any]:
    """Translate FastAPI request/response into the structure expected by auth0_fastapi."""
    return {"request": request, "response": response}


def _merge_set_cookie(source: Response, target: Response) -> None:
    """Copy Set-Cookie headers from the Auth0 helper onto the outgoing response."""
    if not source or not target:
        return
    for cookie in source.headers.getlist("set-cookie"):
        target.headers.append("set-cookie", cookie)


async def _fetch_auth0_session(request: Request, response: Response) -> Optional[Dict[str, Any]]:
    """Retrieve the Auth0 session stored in cookies and store it in the session with cached refresh token."""
    try:
        session_state = await auth_client.client.get_session(
            store_options=_store_options(request, response)
        )
        session_state = await auth_client.client.get_session(store_options=_store_options(request, response))
        if session_state:
            user = session_state.get("user") or {}
            user_sub = user.get("sub")
            cached_refresh = REFRESH_TOKEN_CACHE.get(user_sub or "")
            if cached_refresh:
                session_state.setdefault("refresh_token", cached_refresh)
    except Exception as exc:  # noqa: BLE001
        logging.exception("Failed to retrieve Auth0 session: %s", exc)
        return None
   


def _select_refresh_token(state_data: Dict[str, Any]) -> Optional[str]:
    refresh_token = state_data.get("refresh_token")
    if refresh_token:
        return refresh_token
    for token_set in state_data.get("token_sets") or []:
        if isinstance(token_set, dict) and token_set.get("refresh_token"):
            return token_set["refresh_token"]
    return None


async def fetch_federated_tokens(request: Request, access_token: Optional[str] = None) -> Tuple[Dict[str, Any], Response]:
    """Load linked accounts and exchange for provider-specific tokens."""
    state_response = Response()
    
    # 1. Get token from session if not provided
    if not access_token:
        access_token = request.session.get("access_token")

    if not access_token:
        logging.error("No access token found in session for federated token fetch")
        return {"error": "no_access_token_available"}, state_response

    # 2. Fetch connected accounts
    try:
        # The MyAccount API needs a token audienced/scoped for itself, not the
        # session's general access_token (same fix as start_connect_account below).
        myaccount_token = await auth_client.client.get_access_token(
            audience=f"{MYACCOUNT_BASE_URL}/me/",
            scope="read:me:connected_accounts",
            store_options=_store_options(request, state_response),
        )
        url = f"{MYACCOUNT_BASE_URL}/me/v1/connected-accounts/accounts"
        headers = {"Authorization": f"Bearer {myaccount_token}"}

        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        # The API returns a list or an object with an 'accounts' key
        connected_accounts = data.get("accounts", []) if isinstance(data, dict) else data
        logging.info(f"Found {len(connected_accounts)} connected accounts.")
    except Exception as exc:
        logging.error(f"Failed to fetch connected accounts: {exc}")
        return {"error": "api_request_failed", "details": str(exc)}, state_response

    # 3. Exchange Primary Token for Connection Tokens
    federated_tokens = []
    for account in connected_accounts:
        # Some providers use 'provider', some use 'connection'
        conn_name = account.get("connection") or account.get("provider")
        identity = account.get("identity") or {}
        user_id = identity.get("user_id")

        if not conn_name:
            continue

        try:
            # Exchange the primary token for the target connection token
            token_payload = await auth_client.client.get_access_token_for_connection(
                {"connection": conn_name, "login_hint": user_id,"scope":AUTH0_SCOPE},
                store_options=_store_options(request, state_response),
            )
            federated_tokens.append({"connection": conn_name, "token": token_payload})
        except Exception as exc:
            logging.error(f"Token exchange failed for {conn_name}: {exc}")
            
    return {
        "connected_accounts": connected_accounts,
        "federated_tokens": federated_tokens,
    }, state_response

def requires_auth(handler):
    """Guard routes that require an authenticated profile in the session."""
    @wraps(handler)
    async def wrapped(request: Request, *args, **kwargs):
        if not request.session.get("profile"):
            return RedirectResponse(url="/login", status_code=302)
        return await handler(request, *args, **kwargs)

    return wrapped


async def get_tokenset(request: Request) -> Tuple[Dict[str, Any], Response]:
    """Convenience wrapper used by the token vault API."""
    return await fetch_federated_tokens(request)


def store_session_data(
    session_id: str,
    refresh_token: Optional[str],
    federated_token: Optional[str],
    connection_name: Optional[str],
    user_data: Dict[str, Any],
    access_token: Optional[str] = None,
    connected_accounts: Optional[Any] = None,
) -> None:
    """Persist the consolidated session payload for later agent invocations."""
    if not SESSION_TABLE_NAME:
        logging.debug("DynamoDB session table not configured; skipping persistence.")
        return
    try:
        table = dynamodb.Table(SESSION_TABLE_NAME)

        ttl = int(time.time()) + (24 * 60 * 60)

        item: Dict[str, Any] = {
            "session_id": session_id,
            "ttl": ttl,
            "created_at": int(time.time()),
            "user_id": user_data.get("user_id"),
            "user_email": user_data.get("email"),
            "user_name": user_data.get("name"),
            "user_picture": user_data.get("picture"),
            "federated_token": federated_token,
            "connection_name": connection_name,
            "connected_accounts": connected_accounts
        }

        if refresh_token:
            item["refresh_token"] = refresh_token
        if access_token:
            item["access_token"] = access_token

        table.put_item(Item=item)
        logging.debug("Stored session data for session_id: %s", session_id)

    except Exception as exc:  # noqa: BLE001
        logging.error("Error storing session data: %s", exc)
        raise


def get_session_data(session_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the stored session blob for the chat workflow."""
    if not SESSION_TABLE_NAME:
        logging.debug("DynamoDB session table not configured; skipping lookup.")
        return None
    try:
        table = dynamodb.Table(SESSION_TABLE_NAME)
        response = table.get_item(Key={"session_id": session_id})
        item = response.get("Item")

        if item:
            item.pop("ttl", None)
            return item
        return None

    except Exception as exc:  # noqa: BLE001
        logging.error("Error retrieving session data for %s: %s", session_id, exc)
        return None

@app.get("/")
async def index(request: Request):
    """Render login screen or redirect to chat when already authenticated."""
    if request.session.get("profile"):
        return RedirectResponse(url="/chat", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "session": request.session,
        },
    )


@app.get("/login")
async def login(request: Request):
    """Start interactive Auth0 login and reset any stale session state."""
    request.session.clear()
    temp_response = Response()
    try:
        auth_url = await auth_client.start_login(
            app_state={"returnTo": f"{APP_BASE_URL}/chat"},
            authorization_params=AUTH0_AUTH_PARAMS,
            store_options=_store_options(request, temp_response),
        )
    except Exception as exc:  # noqa: BLE001
        logging.exception("Failed to start Auth0 login: %s", exc)
        return RedirectResponse(url="/", status_code=302)

    outgoing = RedirectResponse(url=auth_url, status_code=302)
    _merge_set_cookie(temp_response, outgoing)
    return outgoing


@app.get("/auth/callback")
async def callback(request: Request):
    """Handle Auth0 login callback and kick off connect-account flow when needed."""
    response = Response()
    # Check if this is a secondary redirect from the Connect Account flow
    if "connect_code" in request.query_params:
        target = f"{APP_BASE_URL}/connect-account/callback?{request.url.query}"
        outgoing = RedirectResponse(url=target, status_code=302)
        _merge_set_cookie(response, outgoing)
        return outgoing
    try:
        result = await auth_client.complete_login(
            str(request.url),
            store_options=_store_options(request, response),
        )
    except Exception as exc:
        logging.exception("Error completing Auth0 login: %s", exc)
        return RedirectResponse(url="/login", status_code=302)

    state_data = result.get("state_data") or {}
    token_sets = state_data.get("token_sets") or []
    primary_token = token_sets[0].get("access_token") if token_sets else None
    # Store essential data in session immediately
    userinfo = state_data.get("user") or {}
    request.session["profile"] = {
        "user_id": userinfo.get("sub"),
        "name": userinfo.get("name"),
        "email": userinfo.get("email"),
    }
    request.session["access_token"] = primary_token
    request.session["session_id"] = str(uuid.uuid4())

    # PREPARE REDIRECT: Go to Connect Flow instead of Chat
    outgoing = RedirectResponse(url="/connect-account/start", status_code=302)
    _merge_set_cookie(response, outgoing)
    return outgoing




@app.get("/login")
async def login(request: Request):
    response = Response()
    auth_url = await auth_client.start_login(
        app_state={"returnTo": f"{APP_BASE_URL}/"},
        authorization_params=AUTH0_AUTH_PARAMS,
        store_options=_store_options(request, response),
    )
    outgoing = RedirectResponse(url=auth_url, status_code=302)
    _merge_set_cookie(response, outgoing)
    return outgoing
    
@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    temp_response = Response()
    try:
        logout_url = await auth_client.logout(
            return_to=f"{APP_BASE_URL}/",
            store_options=_store_options(request, temp_response),
        )
    except Exception as exc:  # noqa: BLE001
        logging.exception("Auth0 logout failed: %s", exc)
        return RedirectResponse(url="/", status_code=302)

    outgoing = RedirectResponse(url=logout_url, status_code=302)
    _merge_set_cookie(temp_response, outgoing)
    return outgoing


@app.get("/connect-account/start")
@requires_auth
async def connect_account_start(request: Request):
    """Begin the Auth0 connect-account flow using the primary session profile."""
    temp_response = Response()
    
    # Get profile from session (populated in /auth/callback)
    profile = request.session.get("profile", {})
    login_hint = profile.get("email")

    connection = AUTH0_CONNECTION_NAME
    scopes = CONNECTED_ACCOUNT_SCOPE.split()

    try:
        # This triggers the Auth0 Account Linking flow
        connect_url = await auth_client.start_connect_account(
            connection=connection,
            scopes=scopes,
            app_state={"returnTo": f"{APP_BASE_URL}/chat"},
            authorization_params={"login_hint": login_hint} if login_hint else None,
            store_options=_store_options(request, temp_response),
        )
    except Exception as exc:
        logging.exception("Automatic connect start failed: %s", exc)
        return RedirectResponse(url="/chat", status_code=302)

    outgoing = RedirectResponse(url=connect_url, status_code=302)
    _merge_set_cookie(temp_response, outgoing)
    return outgoing


@app.get("/connect-account/callback")
@requires_auth
async def connect_account_callback(request: Request):
    """Complete the connect-account flow and persist newly acquired tokens."""
    temp_response = Response()
    try:
        await auth_client.complete_connect_account(
            str(request.url),
            store_options=_store_options(request, temp_response),
        )
        request.session["connected_account_status"] = True
        
        # Now fetch the federated tokens for DynamoDB
        primary_token = request.session.get("access_token")
        tokens_payload, _ = await fetch_federated_tokens(request, access_token=primary_token)
        federated_entries = tokens_payload.get("federated_tokens") or []
        federated_token_value = None
        if isinstance(federated_entries, list) and federated_entries:
            first_entry = federated_entries[0]
            if isinstance(first_entry, dict):
                federated_token_value = first_entry.get("token")
        # Persist everything to DynamoDB
        store_session_data(
            session_id=request.session["session_id"],
            refresh_token=request.session.get("refresh_token"),
            federated_token=federated_token_value,
            connection_name=AUTH0_CONNECTION_NAME,
            user_data=request.session["profile"],
            access_token=primary_token,
            connected_accounts=tokens_payload.get("connected_accounts")
        )

    except Exception as exc:
        logging.exception("Connect callback failed: %s", exc)
        request.session["connected_account_status"] = False
  
    outgoing = RedirectResponse(url="/chat", status_code=302)
    _merge_set_cookie(temp_response, outgoing)
    return outgoing

@app.api_route("/chat", methods=["GET", "POST"])
@requires_auth
async def chat_page(request: Request):
    """Render or post chat messages while proxying invocations to AgentCore."""
    session_store = request.session
    messages = list(session_store.get("chat_messages", []))

    accept_header = (request.headers.get("accept") or "").lower()
    wants_json = (
        "application/json" in accept_header
        or request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.query_params.get("format") == "json"
    )

    if request.method == "POST":
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            payload = await request.json()
            user_message = (payload.get("message") or "").strip()
        else:
            form = await request.form()
            user_message = (form.get("message") or "").strip()

        if user_message:
            messages.append(
                {
                    "sender": "user",
                    "message": user_message,
                    "timestamp": "Just now",
                }
            )
            session_store["chat_messages"] = messages

            try:
                session_id = session_store.get("session_id")
                if not session_id:
                    error_payload = {
                        "response": "No session ID found. Please log in again.",
                        "success": False,
                    }
                    if wants_json:
                        return JSONResponse(error_payload, status_code=401)
                    raise HTTPException(status_code=401, detail=error_payload["response"])

                session_data = get_session_data(session_id)
                if not session_data:
                    error_payload = {
                        "response": "Session expired or invalid. Please log in again.",
                        "success": False,
                    }
                    if wants_json:
                        return JSONResponse(error_payload, status_code=401)
                    raise HTTPException(status_code=401, detail=error_payload["response"])

                #bearer_token = get_bearer_token()
                bearer_token = session_store.get("access_token")
                agent_runtime_arn_encoded = urllib.parse.quote(AGENT_RUNTIME_ARN, safe="")
                
                invoke_agent_arn=os.getenv("AGENT_RUNTIME_ARN")
                

                escaped_agent_arn = urllib.parse.quote(invoke_agent_arn, safe='')
                url = f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com/runtimes/{escaped_agent_arn}/invocations?qualifier=DEFAULT"

                headers = {
                    "Authorization": f"Bearer {bearer_token}",
                    "Content-Type": "application/json",
                    # AWS requires runtimeSessionId to be >= 33 chars - Auth0's user_id
                    # ("auth0|" + 24 hex chars = 30 chars) is too short. session_id is a
                    # UUID4 (36 chars) and is already validated non-empty above.
                    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
                }
                response = requests.post(
                    url,
                    headers=headers,
                    data=json.dumps(
                        {
                            "prompt": user_message,
                            "dynamoID": session_id,
                            "email": session_store.get("profile", {}).get("email"),
                        }
                    ),
                    timeout=30,
                )
                response.raise_for_status()
                agent_response = response.json()
                response_text = extract_response_text(agent_response)

                messages.append(
                    {
                        "sender": "agent",
                        "message": response_text,
                        "timestamp": "Just now",
                    }
                )
                session_store["chat_messages"] = messages

                if wants_json:
                    return JSONResponse({"response": response_text, "success": True})

            except requests.exceptions.HTTPError as exc:
                logging.exception(
                    "Agent Core API error: %s | response body: %s",
                    exc,
                    exc.response.text if exc.response is not None else "<no response>",
                )
                error_msg = f"Agent Core API error: {str(exc)}"
                if wants_json:
                    return JSONResponse({"response": error_msg, "success": False}, status_code=500)
                messages.append(
                    {
                        "sender": "system",
                        "message": error_msg,
                        "timestamp": "Just now",
                    }
                )
                session_store["chat_messages"] = messages

            except Exception as exc:  # noqa: BLE001
                logging.exception("Error in chat: %s", exc)
                error_msg = f"Internal error: {str(exc)}"
                if wants_json:
                    return JSONResponse({"response": error_msg, "success": False}, status_code=500)
                messages.append(
                    {
                        "sender": "system",
                        "message": error_msg,
                        "timestamp": "Just now",
                    }
                )
                session_store["chat_messages"] = messages

    connected_status = session_store.get("connected_account_status", False)
    connect_account_url = request.url_for("connect_account_start")

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "user": session_store.get("profile"),
            "messages": messages,
            "connected_status": connected_status,
            "connect_account_url": connect_account_url,
            "session": session_store,
        },
    )


@app.post("/clear-chat")
@requires_auth
async def clear_chat(request: Request):
    request.session["chat_messages"] = []
    return RedirectResponse(url="/chat", status_code=302)


@app.get("/api/token-vault")
@requires_auth
async def get_token_vault_data(request: Request):
    try:
        tokenset, store_response = await get_tokenset(request)

        if not tokenset:
            response = JSONResponse(
                {
                    "error": "token_fetch_failed",
                    "message": "Token vault is empty. Connect an account to retrieve federated tokens.",
                },
                status_code=400,
            )
            _merge_set_cookie(store_response, response)
            return response

        if tokenset.get("error"):
            status = tokenset.get("status", 400)
            response = JSONResponse(tokenset, status_code=status)
            _merge_set_cookie(store_response, response)
            return response

        vault_data = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user": request.session.get("profile", {}).get("email", "Unknown"),
            "connected_accounts": tokenset.get("connected_accounts", []),
            "federated_tokens": tokenset.get("federated_tokens", []),
            "connection": AUTH0_CONNECTION_NAME or "Not configured",
            "audience": f"https://{AUTH0_DOMAIN}/api/v2/",
            "connected_account_status": request.session.get("connected_account_status", False),
        }

        session_id = request.session.get("session_id")
        if session_id and SESSION_TABLE_NAME:
            try:
                store_session_data(
                    session_id=session_id,
                    refresh_token=request.session.get("refresh_token"),
                    federated_token=None,
                    connection_name=None,
                    user_data=request.session.get("profile", {}),
                    access_token=request.session.get("access_token"),
                    connected_accounts=vault_data["connected_accounts"],
                )
            except Exception as exc:  # noqa: BLE001
                logging.warning("Unable to refresh Dynamo session record: %s", exc)

        response = JSONResponse(vault_data)
        _merge_set_cookie(store_response, response)
        return response

    except Exception as exc:  # noqa: BLE001
        logging.exception("Error fetching token vault data: %s", exc)
        return JSONResponse(
            {
                "error": str(exc),
                "message": "Failed to fetch token vault data",
            },
            status_code=500,
        )
 
def extract_response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return (
            response.get("text")
            or response.get("message")
            or response.get("content")
            or str(response)
        )
    return "Received response from agent."


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=5000, reload=True)
