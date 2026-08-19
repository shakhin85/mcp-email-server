@echo off
REM Wrapper: pulls the password from a secret file; env comes from the MCP config.
REM The password never lives in the config and never passes through the agent.
setlocal
set "SECRET=%USERPROFILE%\.config\mcp-email\password"
if not exist "%SECRET%" (
  echo run.cmd: secret file not found: %SECRET% 1>&2
  exit /b 1
)
set /p MCP_EMAIL_SERVER_PASSWORD=<"%SECRET%"
uv run --directory "%~dp0." mcp-email-server stdio
