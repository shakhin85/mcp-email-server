#!/usr/bin/env bash
# Wrapper: pulls the password from a secret file; env comes from the MCP config.
# The password never lives in the config and never passes through the agent.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
secret="${MCP_EMAIL_SERVER_PASSWORD_FILE:-$HOME/.config/mcp-email/password}"
if [[ ! -f "$secret" ]]; then
  echo "run.sh: secret file not found: $secret" >&2
  exit 1
fi
export MCP_EMAIL_SERVER_PASSWORD="$(<"$secret")"
exec uv run --directory "$here" mcp-email-server stdio
