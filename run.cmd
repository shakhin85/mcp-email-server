@echo off
REM Wrapper: подтягивает пароль из secret-файла, env — из конфига MCP.
REM Пароль НЕ хранится в конфиге и не проходит через агента.
setlocal
set "SECRET=%USERPROFILE%\.config\mcp-email\password"
if not exist "%SECRET%" (
  echo run.cmd: secret file not found: %SECRET% 1>&2
  exit /b 1
)
for /f "usebackq delims=" %%p in ("%SECRET%") do set "MCP_EMAIL_SERVER_PASSWORD=%%p"
uv run --directory "%~dp0." mcp-email-server stdio
