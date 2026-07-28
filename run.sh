#!/usr/bin/env bash
# Wrapper: подтягивает пароль из secret-файла (chmod 600), env — из ~/.claude.json.
# Пароль НЕ хранится в конфиге и не проходит через агента.
set -euo pipefail
export MCP_EMAIL_SERVER_PASSWORD="$(cat "$HOME/.config/mcp-email/password")"
exec uv run --directory /home/linux_admin/projects/mcp-email-server mcp-email-server stdio
