# AgentCore Deployment

This folder holds the agent code and deployment helpers. The documentation below only covers files that live directly in `agentCoreDeployment/` 

## Key Files

- `agentcore_agent.py` — Agent entrypoint script used for registration flows.
- `agentcore_deployment.py` — Deployment helper for launching the agent runtime.
- `env.sample` — Template for the `.env` file expected when running deployment scripts locally. Copy to `.env` and replace placeholders with real values.
- `requirements.txt` — Python dependencies needed for the register agent tooling.
- `Dockerfile` — Container image definition for packaging the register agent runtime.
- `README.md` — This documentation file.

## Prerequisites

- Python environment with dependencies from `requirements.txt` installed.
- AWS credentials with permission to assume the AgentCore execution role, access ECR, and DynamoDB.
- Auth0 tenant configured for both standard OAuth flows and CIBA.

## Setup

1. Copy `env.sample` to `.env` in this directory and populate every value, including AWS,
   Auth0, CIBA, FGA, and Okta settings:
   ```bash
   cp env.sample .env
   # edit .env to add AWS, Auth0, CIBA, FGA, and Okta values
   ```
   `agentcore_deployment.py` reads these from `.env` and pushes them into the deployed
   AgentCore Runtime's `environment_variables` config — there is no AWS Secrets Manager
   step in this lab. `.env` itself is excluded from the Docker build via `.dockerignore`,
   so it never ends up baked into the container image.

## Deploying with AgentCore

Run the deployment helper from this folder once the `.env` file is ready:
```bash
python agentcore_deployment.py
```
The script will:
- Validate AWS credentials.
- Configure the AgentCore runtime for the `agentcore_agent.py` entrypoint.
- Launch the runtime (auto-creating the execution role and ECR repository if needed).

## Running the Agent Entrypoint

`agentcore_agent.py` is invoked by the AgentCore runtime, but you can run it locally for debugging:
```bash
python agentcore_agent.py
```
Ensure that:
- The `.env` is loaded (handled automatically at import).
- The Secrets Manager secret is accessible from your AWS credentials.
- DynamoDB table `auth0_agentcore_agent` exists and stores session records referenced by the tools.

## Notes

- Avoid committing real credentials; only the sample `.env` should live in source control.
- Logging is configured at `INFO` level. Adjust as needed if you require more verbose diagnostics.
- If you add new tools or remote MCP integrations, update both the agent entrypoint and any documentation here to reflect the change.

