import json
import os
import time

import requests
import boto3
from dotenv import load_dotenv
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent, tool
from strands.models import BedrockModel
from bedrock_agentcore import BedrockAgentCoreApp
import asyncio
from openfga_sdk.client import OpenFgaClient, ClientConfiguration
from openfga_sdk.client.models import ClientCheckRequest
from openfga_sdk.credentials import Credentials, CredentialConfiguration
from strands.tools.executors import SequentialToolExecutor
import logging

from strands.tools.mcp import MCPClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)



BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# --- Globals for session context ---
dynamodbSessionID = ""
email = ""
access_token=""
# -----------------------------------

app = BedrockAgentCoreApp()

# Auth0, FGA, and Okta settings — read directly from the environment. Locally these come
# from agentCoreDeployment/.env; when deployed, AgentCore Runtime injects them via its own
# environment_variables config (set by agentcore_deployment.py at deploy time).
# CIBA reuses the same Auth0 application as the main login/JWT-authorizer flow (per the
# blog: ticking the Token Vault + CIBA grant checkboxes on that one app) — so these read
# straight off AUTH0_DOMAIN/AUTH0_CLIENT_ID/AUTH0_CLIENT_SECRET rather than separate vars.
CIBA_AUTH0_DOMAIN = (
    os.getenv("AUTH0_DOMAIN", "")
    .replace("https://", "")
    .replace("http://", "")
    .strip("/")
)
if not CIBA_AUTH0_DOMAIN:
    raise ValueError("AUTH0_DOMAIN must be configured via the .env file.")

CIBA_CLIENT_ID = os.getenv("AUTH0_CLIENT_ID", "")
CIBA_CLIENT_SECRET = os.getenv("AUTH0_CLIENT_SECRET", "")
CIBA_SCOPE = os.getenv("CIBA_SCOPE", "openid profile")
DEFAULT_BINDING_MESSAGE = os.getenv("CIBA_BINDING_MESSAGE", "RESET PASSWORD FLOW")
ciba_url = f"https://{CIBA_AUTH0_DOMAIN}/bc-authorize"
token_url = f"https://{CIBA_AUTH0_DOMAIN}/oauth/token"

FGA_API_TOKEN_ISSUER = os.getenv("FGA_API_TOKEN_ISSUER", "")
FGA_API_AUDIENCE = os.getenv("FGA_API_AUDIENCE", "")
FGA_CLIENT_ID = os.getenv("FGA_CLIENT_ID", "")
FGA_CLIENT_SECRET = os.getenv("FGA_CLIENT_SECRET", "")
# Not part of FGA's own config output (always "https" in practice) — kept as its own
# var since the OpenFGA SDK wants scheme and host passed separately.
FGA_API_SCHEME = os.getenv("FGA_API_SCHEME", "https")
FGA_API_HOST = (
    os.getenv("FGA_API_URL", "")
    .replace("https://", "")
    .replace("http://", "")
    .strip("/")
)
FGA_STORE_ID = os.getenv("FGA_STORE_ID", "")
FGA_MODEL_ID = os.getenv("FGA_MODEL_ID", "")
MCP_GATEWAY_URL = os.getenv("MCP_GATEWAY_URL")
OKTA_DOMAIN = (
    os.getenv("OKTA_DOMAIN", "kapil.oktapreview.com")
    .replace("https://", "")
    .replace("http://", "")
    .strip("/")
)


logger.info("CIBA_CLIENT_ID: %s", CIBA_CLIENT_ID) 
# --- DynamoDB Helper ---
def get_dynamodb_table(region=None):
    """Helper function to get the DynamoDB table resource."""
    table_name = os.getenv("SESSION_TABLE_NAME")
    if not table_name:
        raise ValueError("SESSION_TABLE_NAME not configured")
    dynamodb = boto3.resource("dynamodb", region_name=region or os.getenv("AWS_REGION", "us-east-1"))
    return dynamodb.Table(table_name)
# ----------------------

async def main(user_obj):
    """
    Perform FGA authorization check for the given user object.
    (This function appears unchanged from your original)
    """
    # Step 1: Set up client credentials for Auth0 authentication
    credentials = Credentials(
        method="client_credentials",
        configuration=CredentialConfiguration(
            api_issuer=FGA_API_TOKEN_ISSUER,
            api_audience=FGA_API_AUDIENCE,
            client_id=FGA_CLIENT_ID,
            client_secret=FGA_CLIENT_SECRET,
        )
    )

    configuration = ClientConfiguration(
        api_scheme=FGA_API_SCHEME,
        api_host=FGA_API_HOST,
        store_id=FGA_STORE_ID,
        authorization_model_id=FGA_MODEL_ID,
        credentials=credentials,
    )

    async with OpenFgaClient(configuration) as fga_client:
        # Step 03. Check for access
        options = {}
        body = ClientCheckRequest(
            user='user:' + user_obj['user'],  # e.g., "user:alice@example.com"
            relation=user_obj['relation'],    # e.g., "read"
            object=user_obj['object'],        # e.g., "document:123"
        )
        response = await fga_client.check(body, options)
        return response
        await fga_client.close()


os.environ["LANGSMITH_OTEL_ENABLED"] = "true"

@tool
def weather():
    """Get weather"""
    return "sunny"

# --- REFACTORED TOOL 1 ---

# --- CIBA Password Reset Tool ---

@tool
def invokeCiba(user_identifier: str = "", scope: str = "", binding_message: str = ""):
    """
    Initiate and complete a CIBA password-reset approval flow in one step.
    Provide an explicit user_identifier (Auth0 subject) if available; otherwise we will
    fall back to the email captured in the session payload.
    """
    logger.info("[Tool:invokeCiba] invoked")

    identifier = ""
    
    if not identifier:
        session_id = (dynamodbSessionID or "").strip()
        if not session_id:
            logger.error("Missing session id (dynamoID) and no user identifier provided")
            return json.dumps({"error": "Missing session id and user identifier"})
        table = get_dynamodb_table()
        resp = table.get_item(Key={"session_id": session_id})
        item = resp.get("Item")
        if not item:
            logger.error("No session found for id %s", session_id)
            return json.dumps({"error": f"No session found for id {session_id}"})
        identifier = item.get("user_id", "").strip()

    if not identifier:
        return json.dumps({"error": "No user identifier available for CIBA login_hint"})


    login_hint = {
        "format": "iss_sub",
        "iss": f"https://{CIBA_AUTH0_DOMAIN}/",
        "sub": identifier
    }

    payload = {
        "client_id": CIBA_CLIENT_ID,
        "client_secret": CIBA_CLIENT_SECRET,
        "login_hint": json.dumps(login_hint),
        "scope": scope or CIBA_SCOPE,
        "binding_message": binding_message or DEFAULT_BINDING_MESSAGE,
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        response = requests.post(ciba_url, headers=headers, data=payload)
        try:
            auth_data = response.json()
        except json.JSONDecodeError:
            logger.error("Non-JSON response from CIBA initiation: %s", response.text)

        if response.status_code != 200:
            logger.error("Failed to initiate CIBA request: %s", auth_data)

        auth_req_id = auth_data.get('auth_req_id')
        expires_in = auth_data.get('expires_in', 300)
        interval = auth_data.get('interval', 5)
        logger.info("CIBA initiated. auth_req_id=%s expires_in=%s interval=%s", auth_req_id, expires_in, interval)

        def poll_for_token(token_url: str, auth_req_id: str, expires_in: int, interval: int):
            start_time = time.time()
            current_interval = interval
            while True:
                if time.time() - start_time > expires_in:
                    logger.warning("CIBA request timed out after %s seconds", expires_in)
                    return None

                token_payload = {
                    'grant_type': 'urn:openid:params:grant-type:ciba',
                    'auth_req_id': auth_req_id,
                    'client_id': CIBA_CLIENT_ID,
                    'client_secret': CIBA_CLIENT_SECRET

                }
                token_headers = {"Content-Type": "application/x-www-form-urlencoded"}

                try:
                    token_response = requests.post(token_url, data=token_payload, headers=token_headers)
                    if token_response.status_code == 200:
                        logger.info("CIBA token obtained successfully")
                        return token_response.json()

                    try:
                        error_response = token_response.json()
                    except json.JSONDecodeError:
                        error_response = {"error": token_response.text}

                    error_code = error_response.get('error')
                    if error_code == 'authorization_pending':
                        logger.info('Authorization pending; retrying in %s seconds', current_interval)
                        time.sleep(current_interval)
                        continue
                    if error_code == 'slow_down':
                        current_interval += 5
                        logger.info('Received slow_down; new interval=%s', current_interval)
                        time.sleep(current_interval)
                        continue

                    logger.error('CIBA token polling failed: %s', error_response)
                    return None

                except Exception as poll_exc:
                    logger.error('Error during CIBA token polling: %s', poll_exc)
                    return None

        tokens = poll_for_token(token_url, auth_req_id, expires_in, interval)
        if tokens:
            logger.info("CIBA flow completed for identifier=%s", identifier)
            return json.dumps({
                "status": "success",
                "message": "Identity verified and password has been sucessfully set"
            })
        logger.warning("CIBA flow failed or timed out for identifier=%s", identifier)
        return json.dumps({
            "status": "failed",
            "message": "Unable to verify identity. Please try again."
        })

    except Exception as e:
        logger.error("Exception in invokeCiba: %s", e)
        return json.dumps({"error": f"An internal error occurred: {str(e)}"})


@tool
def getOktaGroups():
    """
    Fetch Okta groups using federated token stored in DynamoDB for the current session.
    Steps:
    1) Read the session item from DynamoDB using the global session id.
    2) Extract the federated access token from the item (key: 'federated_token').
    3) Call the Okta Groups API with that bearer token.
    4) Return the group list as JSON (or a helpful error).
    """
    logger.info("Starting getOktaGroups flow. email=%s", email)
    try:
        user_for_check = {
            "user": email,
            "relation": "read_okta",
            "object": "okta:groups",
        }
        logger.info("Authorization check payload: %s", user_for_check)
        fga_response = asyncio.run(main(user_for_check))
        logger.info("FGA response: %s", fga_response)

        is_authorized = False
        if isinstance(fga_response, dict) and 'Payload' in fga_response:
            response_payload = fga_response['Payload'].read()
            decoded_response = json.loads(response_payload)
            is_authorized = decoded_response.get('isAuthorized') is True
        elif isinstance(fga_response, dict) and 'isAuthorized' in fga_response:
            is_authorized = fga_response.get('isAuthorized') is True
        elif hasattr(fga_response, 'allowed'):
            is_authorized = bool(getattr(fga_response, 'allowed'))

        if not is_authorized:

            return "User not authorized to perform this operation"
    except Exception as e:
        logger.error("error11111: %s", e)
        return json.dumps({"error": "authorization_check_failed", "detail": str(e)})

    session_id = (dynamodbSessionID or "").strip()
    if not session_id:
        return json.dumps({"error": "Missing session id (dynamoID)"})

    try:
        table = get_dynamodb_table()
        resp = table.get_item(Key={"session_id": session_id})
        item = resp.get("Item")
        if not item:
            return json.dumps({"error": f"No session found for id {session_id}"})

        federated_token = item.get("federated_token")
        if not federated_token:
            return json.dumps({"error": "No federated_token found in session item"})

        headers = {"Authorization": f"Bearer {federated_token}", "Accept": "application/json"}

        user_url = f'https://{OKTA_DOMAIN}/api/v1/users/{email}'

        user_response = requests.get(user_url, headers=headers)
        logger.info("User lookup status: %s", user_response.status_code)
        logger.info("User response: %s", user_response.text)
        logger.info("User response headers: %s", dict(user_response.headers))
        if user_response.status_code != 200:
            logger.error("Error retrieving user: %s - %s", user_response.status_code, user_response.text)
            actiongroup_output = f"Error retrieving user: {user_response.status_code}"
        else:
            user = user_response.json()
            user_id = user['id']
            logger.info("User ID: %s", user_id)

            groups_response = requests.get(f'https://{OKTA_DOMAIN}/api/v1/users/{user_id}/groups', headers=headers)

            if groups_response.status_code != 200:
                logger.error("Error retrieving groups: %s - %s", groups_response.status_code, groups_response.text)
                actiongroup_output = f"Error retrieving groups: {groups_response.status_code}"
            else:
                groups = groups_response.json()
                return json.dumps({"okta_groups": groups})

        return json.dumps({"error": actiongroup_output})

    except Exception as e:
        logger.error("Unhandled error fetching Okta groups: %s", e)
        return json.dumps({"error": str(e)})


# --- Agent Definition ---
model_id = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-3-7-sonnet-20250219-v1:0")
model = BedrockModel(
    model_id=model_id,
    streaming=False
)

# Provide a fresh MCP transport per request so auth headers stay current.
def create_transport():
    return streamablehttp_client(
        MCP_GATEWAY_URL,
        headers={"Authorization": f"Bearer {access_token}"}
)


@app.entrypoint
def strands_agent_bedrock(payload):
    """
    Invoke the agent with a payload
    """
    # INSERT_YOUR_CODE
  
    # Log payload fields and stash session identifiers for later tool usage.
    for k, v in payload.items():
        logger.info(f"  {k!r}: {v!r}")
    user_input = payload.get("prompt")
    # Propagate session id and email for tool access
    global dynamodbSessionID, email, access_token
    dynamodbSessionID = str(payload.get("dynamoID") or "")
    email = str(payload.get("email") or "")
    # INSERT_YOUR_CODE
    # Retrieve access_token from DynamoDB session record, if present
    access_token = str(payload.get("access_token") or "")
    try:
        table = get_dynamodb_table()
        resp = table.get_item(Key={"session_id": dynamodbSessionID})
        item = resp.get("Item")
        access_token = item.get("access_token")
    except Exception as e:
        logger.error("Error fetching access_token from DynamoDB: %s", e)

    system_prompt = (
        "You are a helpful assistant with specific tools. Follow these rules carefully:\n"
        "1.  **For Okta groups:** When the user asks to get Okta groups (e.g., 'get me okta groups', 'what are my okta groups'), "
        "you MUST call the `getOktaGroups` tool and return ONLY its result.\n"

        "2.  **For Password Resets:** When a user asks to perform an elevated operation like resetting a password "
        "(e.g., 'reset my password', 'I need to reset a password', 'reset okta password for <email>'), "
        "you MUST call the `invokeCiba` tool, wait for it to complete, and return ONLY the tool's result.\n"

        "3.  **You also have access to **dynamic remote tools**. If a user asks about 'employee tasks', 'assigned work', "
        "or 'employee records', look through your available tools for a match and invoke it immediately.\n"

        "4. **For other tasks:** You can also do simple math calculations and tell the weather."
    )

    try:
        with MCPClient(create_transport) as mcp_client:
            remote_tools = mcp_client.list_tools_sync()
            # Combine local tools with the freshly fetched remote tools
            # We update the agent's tool list for this specific request
            # NOTE: remote tool availability is dynamic per MCP session.
            agent = Agent(
                model=model,
                tools=[weather, getOktaGroups, invokeCiba]+remote_tools,
                system_prompt=system_prompt,
                tool_executor=SequentialToolExecutor()
            )

            resp = agent(user_input)
            return resp.message['content'][0]['text']
    except Exception as e:
        logger.error("MCP Initialization failed: %s", e)
        # Fallback: answer with local tools only, since the MCP/Gateway connection
        # that would have supplied remote_tools never succeeded.
        agent = Agent(
            model=model,
            tools=[weather, getOktaGroups, invokeCiba],
            system_prompt=system_prompt,
            tool_executor=SequentialToolExecutor()
        )
        resp = agent(user_input)
        return resp.message['content'][0]['text']

if __name__ == "__main__":
    app.run()