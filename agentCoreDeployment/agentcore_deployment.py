from dotenv import load_dotenv

# Load environment variables from .env file FIRST
# override=True: values here always win over anything already exported in the shell
# (e.g. a stale AWS_SESSION_TOKEN from an earlier `aws sso login` in the same terminal).
load_dotenv(override=True)

import os
import time

# This script only ever runs locally (never inside the deployed container), so it uses
# an AWS SSO profile (via `aws configure sso`) rather than a hand-copied, rotating
# access-key/secret/session-token triple. AWS_PROFILE being set in the environment is
# also what makes the toolkit's own internal boto3 calls (bare boto3.client(...), not
# using our own Session below) pick up the same profile automatically.
if not os.getenv('AWS_PROFILE'):
    raise ValueError("AWS_PROFILE not found. Set AWS_PROFILE=<your-sso-profile-name> in your .env file (run `aws configure sso` first if you haven't).")

# Now import after credentials are set
# These imports depend on AWS credentials being present.
from bedrock_agentcore_starter_toolkit import Runtime
import traceback
from boto3.session import Session

# Create boto3 session
# Let boto3 discover the region via environment or default to us-east-1.
boto_session = Session(profile_name=os.getenv('AWS_PROFILE'))
region = boto_session.region_name or os.getenv('AWS_DEFAULT_REGION', 'us-east-1')

# Values the agentcore_agent.py entrypoint reads via os.getenv at runtime. Pulled from
# this script's own .env and passed to launch(env_vars=...) so none of it needs to live
# in AWS Secrets Manager or be baked into the container image.
RUNTIME_ENV_VARS = {
    key: os.getenv(key, "")
    for key in [
        "SESSION_TABLE_NAME",
        "AUTH0_DOMAIN",
        "AUTH0_CLIENT_ID",
        "AUTH0_CLIENT_SECRET",
        "CIBA_SCOPE",
        "CIBA_BINDING_MESSAGE",
        "FGA_API_TOKEN_ISSUER",
        "FGA_API_AUDIENCE",
        "FGA_CLIENT_ID",
        "FGA_CLIENT_SECRET",
        "FGA_API_SCHEME",
        "FGA_API_URL",
        "FGA_STORE_ID",
        "FGA_MODEL_ID",
        "MCP_GATEWAY_URL",
        "OKTA_DOMAIN",
        "BEDROCK_MODEL_ID",
    ]
}
RUNTIME_ENV_VARS["AWS_REGION"] = region

# Instantiate the AgentCore runtime helper used to configure and launch deployments.
agentcore_runtime = Runtime()
agent_name = "agentcore_agent_a4aa"

# Configure the AgentCore deployment for the agentcore_agent entrypoint.
response = agentcore_runtime.configure(
    entrypoint="agentcore_agent.py",
    auto_create_execution_role=True,
    auto_create_ecr=True,
    requirements_file="requirements.txt",
    region=region,
    agent_name=agent_name,
    authorizer_configuration={
        "customJWTAuthorizer": {
            "discoveryUrl": f"https://{os.getenv("AUTH0_DOMAIN")}/.well-known/openid-configuration",
            # No allowedClients: AWS validates that against a literal "client_id" claim,
            # which Auth0 access tokens never include (Auth0 uses "azp" instead) - this
            # check can never pass, so allowedAudience alone is the actual security gate.
            "allowedAudience": [os.getenv("AUTH0_AUDIENCE")]
        }
    }
)

try:
    # Trigger the deployment for the agentcore_agent configuration.
    launch_result = agentcore_runtime.launch(env_vars=RUNTIME_ENV_VARS)
    print('AGENT_RUNTIME_ARN:', launch_result.agent_arn)
except Exception as e:
    print('Error launching AgentCore runtime:', repr(e))
    print('Traceback:')
    traceback.print_exc()
    raise
