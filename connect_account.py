"""
Minimal FastAPI script that demonstrates Auth0 login plus Connected Accounts.

Run locally (after setting the required environment variables):

    uvicorn simple_connected_account_app:app --reload --port 8000

Required environment variables:
    AUTH0_DOMAIN
    AUTH0_CLIENT_ID
    AUTH0_CLIENT_SECRET
    AUTH0_SECRET              # used to encrypt cookies

Optional:
    APP_BASE_URL              # defaults to http://127.0.0.1:8000
    AUTH0_AUDIENCE            # e.g. https://<tenant>/me/
    AUTH0_SCOPE               # default: openid profile email offline_access
    CONNECTED_ACCOUNT_SCOPE   # default: myaccount:manage_connections
    AUTH0_CONNECTION_NAME     # default connection for /connect-account/start
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests
from auth0_fastapi.auth import AuthClient
from auth0_fastapi.config import Auth0Config
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from dotenv import load_dotenv
load_dotenv()

from starlette.datastructures import MutableHeaders


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set in the environment.")
    return value


def _merge_set_cookie(source: Response, target: Response) -> None:
    """Copy Set-Cookie headers emitted by the Auth0 helper onto the outgoing response."""
    source_headers: MutableHeaders = source.headers  # type: ignore[assignment]
    target_headers: MutableHeaders = target.headers  # type: ignore[assignment]
    for cookie in source_headers.getlist("set-cookie"):
        target_headers.append("set-cookie", cookie)


AUTH0_DOMAIN = require_env("AUTH0_DOMAIN")
AUTH0_CLIENT_ID = require_env("AUTH0_CLIENT_ID")
AUTH0_CLIENT_SECRET = require_env("AUTH0_CLIENT_SECRET")
AUTH0_SECRET = require_env("AUTH0_SECRET")

APP_BASE_URL =  os.getenv("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
AUTH0_AUDIENCE = os.getenv("AUTH0_AUDIENCE")
AUTH0_SCOPE = os.getenv(
    "AUTH0_SCOPE",
    "openid profile email offline_access read:me:connected_accounts create:me:connected_accounts",
)
CONNECTED_ACCOUNT_SCOPE = os.getenv(
    "CONNECTED_ACCOUNT_SCOPE",
    "openid profile email offline_access"
)
AUTH0_CONNECTION_NAME = os.getenv("AUTH0_CONNECTION_NAME")

AUTH0_BASE_URL = f"https://{AUTH0_DOMAIN}"
MYACCOUNT_BASE_URL = AUTH0_BASE_URL
AUTH0_AUTH_PARAMS = {
    "scope": AUTH0_SCOPE,
    "audience": AUTH0_AUDIENCE,
    "prompt": "consent",
    "access_type": "offline",
}
auth_config = Auth0Config(
    domain=AUTH0_DOMAIN,
    client_id=AUTH0_CLIENT_ID,
    client_secret=AUTH0_CLIENT_SECRET,
    secret=AUTH0_SECRET,
    app_base_url=APP_BASE_URL,
    audience=AUTH0_AUDIENCE,
    callback_path="/callback",
    connect_account_callback_path="/connect-account/callback",
    authorization_params=AUTH0_AUTH_PARAMS
)


auth_client = AuthClient(auth_config)
 
app = FastAPI(title="Auth0 Connected Accounts Demo")

REFRESH_TOKEN_CACHE: Dict[str, str] = {}

def _store_options(request: Request, response: Response) -> Dict[str, Any]:
    """Translate FastAPI request/response into the structure required by AuthClient."""
    return {"request": request, "response": response}


async def _get_session(request: Request, response: Response) -> Optional[Dict[str, Any]]:
    """Retrieve the current Auth0 session stored in cookies."""
    session_state = await auth_client.client.get_session(store_options=_store_options(request, response))
    if session_state:
        user = session_state.get("user") or {}
        user_sub = user.get("sub")
        cached_refresh = REFRESH_TOKEN_CACHE.get(user_sub or "")
        if cached_refresh:
            session_state.setdefault("refresh_token", cached_refresh)
    return session_state


def _user_request(method: str, path: str, token: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    """
    Minimal helper that proxies requests to Auth0 APIs using the user's access token.
    Keeps the implementation close to the original Flask sample for readability.
    """
    base_url = MYACCOUNT_BASE_URL if path.startswith("/me/") else AUTH0_BASE_URL
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    response = requests.request(method, url, json=payload, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


@app.get("/")
async def index(request: Request):
    response = Response()
    session_state = await _get_session(request, response)
    if not session_state:
        payload = {
            "authenticated": False,
            "login": f"{APP_BASE_URL}/login",
        }
        outgoing = JSONResponse(payload)
        _merge_set_cookie(response, outgoing)
        return outgoing

    profile = session_state.get("user") or {}
    refresh_token = session_state.get("refresh_token") or ""
    payload = {
        "authenticated": True,
        "profile": profile,
        "refresh_token_present": bool(refresh_token),
        "links": {
            "login": f"{APP_BASE_URL}/login",
            "logout": f"{APP_BASE_URL}/logout",
            "connect_start": f"{APP_BASE_URL}/connect-account/start",
            "connect_callback": f"{APP_BASE_URL}/connect-account/callback",
            "federated_tokens": f"{APP_BASE_URL}/federated-tokens",
        },
    }
    outgoing = JSONResponse(payload)
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


@app.get("/auth/callback")
async def callback(request: Request):
    print("I am in callback callback callback callback callback")
    response = Response()
    if "connect_code" in request.query_params:
        redirect_target = f"{APP_BASE_URL}/connect-account/callback?{request.url.query}"
        outgoing = RedirectResponse(url=redirect_target, status_code=302)
        _merge_set_cookie(response, outgoing)
        return outgoing
    try:
        result = await auth_client.complete_login(
            str(request.url),
            store_options=_store_options(request, response),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"callback_failed: {exc}") from exc

    state_data = result.get("state_data") or {}
    refresh_token = state_data.get("refresh_token")
    print('refresh_tokenrefresh_tokenrefresh_token',refresh_token)
    if not refresh_token:
        token_sets = state_data.get("token_sets") or []
        for token_set in token_sets:
            if isinstance(token_set, dict):
                token_refresh = token_set.get("refresh_token")
                if token_refresh:
                    refresh_token = token_refresh
                    break
    user_info = state_data.get("user") or {}
    user_sub = user_info.get("sub")
    if refresh_token and user_sub:
        REFRESH_TOKEN_CACHE[user_sub] = refresh_token
        try:
            updated_state = dict(state_data)
            updated_state["refresh_token"] = refresh_token
            await auth_client.client._state_store.set(  # type: ignore[attr-defined]
                auth_client.client._state_identifier,  # type: ignore[attr-defined]
                updated_state,
                options=_store_options(request, response),
            )
        except AttributeError:
            pass

    app_state = result.get("app_state") or {}
    return_to = app_state.get("returnTo") or f"{APP_BASE_URL}/"
    outgoing = RedirectResponse(url=return_to, status_code=302)
    _merge_set_cookie(response, outgoing)
    return outgoing


@app.get("/logout")
async def logout(request: Request):
    response = Response()
    logout_url = await auth_client.logout(
        return_to=f"{APP_BASE_URL}/",
        store_options=_store_options(request, response),
    )
    outgoing = RedirectResponse(url=logout_url, status_code=302)
    _merge_set_cookie(response, outgoing)
    return outgoing


@app.get("/connect-account/start")
async def connect_account_start(request: Request):
    response = Response()
    session_state = await _get_session(request, response)
    print('session_state',session_state)
    if not session_state:
        raise HTTPException(status_code=401, detail="not_authenticated")

    connection = request.query_params.get("connection") or AUTH0_CONNECTION_NAME
    if not connection:
        raise HTTPException(status_code=400, detail="missing_connection")
    scope = request.query_params.get("scope") or CONNECTED_ACCOUNT_SCOPE
    scopes = scope.split()
    login_hint = request.query_params.get("login_hint")
    if not login_hint:
        user_info = session_state.get("user") or {}
        login_hint = (user_info.get("email") or "").strip()
    
    connect_url = await auth_client.start_connect_account(
        connection=connection,
        scopes=scopes,
        app_state={"returnTo": f"{APP_BASE_URL}/"},
        authorization_params={"login_hint": login_hint} if login_hint else None,
        store_options=_store_options(request, response),
    )
    outgoing = RedirectResponse(url=connect_url, status_code=302)

    _merge_set_cookie(response, outgoing)
    return outgoing


@app.get("/connect-account/callback")
async def connect_account_callback(request: Request):
    response = Response()
    session_state = await _get_session(request, response)
    if not session_state:
        raise HTTPException(status_code=401, detail="not_authenticated")

    try:
        complete_response = await auth_client.complete_connect_account(
            str(request.url),
            store_options=_store_options(request, response),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"complete_failed: {exc}") from exc

    payload = {
        "status": "connected",
        "details": complete_response.model_dump(),
    }
    outgoing = JSONResponse(payload)
    _merge_set_cookie(response, outgoing)
    return outgoing


@app.get("/federated-tokens")
async def federated_tokens(request: Request):
    print('federated_tokensfederated_tokensfederated_tokensfederated_tokensfederated_tokens')
    FEDERATED_SCOPE = CONNECTED_ACCOUNT_SCOPE


    response = Response()
    session_state = await _get_session(request, response)
    if not session_state:
        raise HTTPException(status_code=401, detail="not_authenticated")

    token_sets = session_state.get("token_sets") or []
    access_token = token_sets[0].get("access_token") if token_sets else None
    print('acccesstoken',access_token) 
    if not access_token:
        raise HTTPException(status_code=400, detail="missing_access_token")

    try:
        accounts = _user_request(
            "GET",
            "/me/v1/connected-accounts/accounts",
            token=access_token
        )
        print('accounts',accounts)
    except requests.HTTPError as exc:
        print('exc',exc)
        details = {}
        try:
            details = exc.response.json()
        except Exception:
            details = {"status": exc.response.status_code, "text": exc.response.text}
        token_audience = token_sets[0].get("aud") if token_sets else "missing"
        token_scope = token_sets[0].get("scope") if token_sets else "missing"
        print("connected-accounts 404; token audience:", token_audience)
        print("connected-accounts 404; token scope:", token_scope)
        print("response detail:", details)
        raise HTTPException(status_code=exc.response.status_code, detail=details) from exc

    connected_accounts = accounts.get("accounts", []) if isinstance(accounts, dict) else accounts
    federated_tokens: list[Dict[str, Any]] = []
    print('connected_accounts',connected_accounts)

    for account in connected_accounts:
        connection = account.get("connection") or account.get("provider")
        identity = account.get("identity") or {}
        login_hint = identity.get("user_id") if isinstance(identity, dict) else None

        if not connection:
            federated_tokens.append({"error": "missing_connection", "details": account})
            continue

        print('connection',connection)
        print('login_hint',login_hint)
        print('_store_options(request, response)',_store_options(request, response))
        try:
            token = await auth_client.client.get_access_token_for_connection(
                {
                    "connection": connection,
                    "login_hint": login_hint,
                },
                store_options=_store_options(request, response),
            )
            federated_tokens.append({"connection": connection, "token": token})
        except Exception as exc:
            federated_tokens.append(
                {
                    "connection": connection,
                    "error": "exchange_failed",
                    "details": str(exc),
                }
            )

    payload = {
        "connected_accounts": connected_accounts,
        "federated_tokens": federated_tokens,
    }
    outgoing = JSONResponse(payload)
    _merge_set_cookie(response, outgoing)
    return outgoing


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("connect_account:app", host="0.0.0.0", port=5000, reload=True)