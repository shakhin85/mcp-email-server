import asyncio
import ssl
from email.mime.text import MIMEText
from unittest.mock import create_autospec, patch

import aioimaplib
import pytest

from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import (
    EmailClient,
    ImapTransportError,
    _create_starttls_ssl_context,
    _imap_starttls,
    _send_imap_id,
)


class Response:
    def __init__(self, result: str, lines: list[bytes] | None = None):
        self.result = result
        self.lines = lines or []


def _make_imap(capabilities=("IMAP4rev1", "STARTTLS")):
    imap = create_autospec(aioimaplib.IMAP4, instance=True)
    imap.timeout = 10
    imap._client_task = asyncio.Future()
    imap._client_task.set_result(None)
    imap.protocol = create_autospec(aioimaplib.IMAP4ClientProtocol, instance=True)
    imap.protocol.capabilities = set(capabilities)
    imap.protocol.pending_async_commands = {}
    imap.protocol.loop = asyncio.get_event_loop()
    imap.protocol.new_tag.return_value = "A001"
    imap.protocol.execute.return_value = Response("OK")
    imap.protocol.transport = create_autospec(asyncio.Transport, instance=True)
    return imap


def _make_loop(start_tls_result):
    loop = create_autospec(asyncio.AbstractEventLoop, instance=True)
    loop.start_tls.return_value = start_tls_result
    return loop


def test_email_settings_init_accepts_imap_start_ssl():
    settings = EmailSettings.init(
        account_name="test",
        full_name="Test User",
        email_address="test@example.com",
        user_name="test@example.com",
        password="secret",
        imap_host="127.0.0.1",
        imap_port=1143,
        imap_ssl=False,
        imap_start_ssl=True,
        smtp_host="127.0.0.1",
    )

    assert settings.incoming.use_ssl is False
    assert settings.incoming.start_ssl is True


def test_email_settings_from_env_accepts_imap_start_ssl(monkeypatch):
    monkeypatch.setenv("MCP_EMAIL_SERVER_EMAIL_ADDRESS", "test@example.com")
    monkeypatch.setenv("MCP_EMAIL_SERVER_PASSWORD", "secret")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_PORT", "1143")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_SSL", "false")
    monkeypatch.setenv("MCP_EMAIL_SERVER_IMAP_START_SSL", "true")
    monkeypatch.setenv("MCP_EMAIL_SERVER_SMTP_HOST", "127.0.0.1")

    settings = EmailSettings.from_env()

    assert settings is not None
    assert settings.incoming.use_ssl is False
    assert settings.incoming.start_ssl is True


def test_create_starttls_ssl_context_verify_false_is_permissive():
    ctx = _create_starttls_ssl_context(False)

    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


@pytest.mark.asyncio
async def test_send_imap_id_sends_one_compact_command():
    imap = _make_imap(capabilities=(b"imap4rev1", b"id"))

    await _send_imap_id(imap)

    imap.id.assert_not_awaited()
    imap.protocol.execute.assert_awaited_once()
    command = imap.protocol.execute.await_args.args[0]
    assert command.name == "ID"
    assert command.tag == "A001"
    assert command.args == ('("name" "mcp-email-server" "version" "1.0.0")',)
    assert command._timeout == imap.timeout


@pytest.mark.asyncio
async def test_send_imap_id_skips_server_without_capability():
    imap = _make_imap(capabilities=("IMAP4rev1",))

    await _send_imap_id(imap)

    imap.id.assert_not_awaited()
    imap.protocol.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_imap_id_logs_non_ok_response_without_retrying():
    imap = _make_imap(capabilities=("IMAP4rev1", "ID"))
    imap.protocol.execute.return_value = Response("BAD")

    with patch("mcp_email_server.emails.classic.logger.warning") as mock_warning:
        await _send_imap_id(imap)

    imap.protocol.execute.assert_awaited_once()
    mock_warning.assert_called_once_with("IMAP ID command failed")


@pytest.mark.asyncio
async def test_send_imap_id_discards_connection_after_timeout():
    imap = _make_imap(capabilities=("IMAP4rev1", "ID"))
    protocol = imap.protocol

    async def time_out(command: aioimaplib.Command) -> None:
        protocol.pending_async_commands["ID"] = command
        raise aioimaplib.CommandTimeout(command)

    protocol.execute.side_effect = time_out

    with (
        patch("mcp_email_server.emails.classic.logger.warning") as mock_warning,
        pytest.raises(TimeoutError, match="IMAP ID command timed out"),
    ):
        await _send_imap_id(imap)

    assert imap.protocol is None
    assert protocol.pending_async_commands == {}
    protocol.transport.abort.assert_called_once_with()
    mock_warning.assert_called_once_with("IMAP ID command timed out")


@pytest.mark.asyncio
async def test_send_imap_id_discards_connection_on_cancellation():
    imap = _make_imap(capabilities=("IMAP4rev1", "ID"))
    protocol = imap.protocol
    command_started = asyncio.Event()

    async def wait_for_response(command: aioimaplib.Command) -> None:
        protocol.pending_async_commands["ID"] = command
        command_started.set()
        await asyncio.Future()

    protocol.execute.side_effect = wait_for_response
    operation = asyncio.create_task(_send_imap_id(imap))
    await command_started.wait()
    operation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await operation

    assert imap.protocol is None
    assert protocol.pending_async_commands == {}
    protocol.transport.abort.assert_called_once_with()


@pytest.mark.asyncio
async def test_send_imap_id_discards_connection_after_transport_failure():
    imap = _make_imap(capabilities=("IMAP4rev1", "ID"))
    protocol = imap.protocol
    protocol.execute.side_effect = ConnectionError("provider detail")

    with (
        patch("mcp_email_server.emails.classic.logger.warning") as mock_warning,
        pytest.raises(ImapTransportError, match=r"IMAP ID command failed \(TRANSPORT\)"),
    ):
        await _send_imap_id(imap)

    assert imap.protocol is None
    protocol.transport.abort.assert_called_once_with()
    mock_warning.assert_called_once_with("IMAP ID command failed")


@pytest.mark.asyncio
async def test_send_imap_id_handles_missing_protocol():
    imap = create_autospec(aioimaplib.IMAP4, instance=True)
    imap.protocol = None

    with patch("mcp_email_server.emails.classic.logger.warning") as mock_warning:
        await _send_imap_id(imap)

    imap.id.assert_not_awaited()
    mock_warning.assert_called_once_with("IMAP ID command failed: IMAP protocol is not connected")


@pytest.mark.asyncio
async def test_imap_starttls_upgrades_transport_and_refreshes_capabilities():
    imap = _make_imap()
    tls_transport = create_autospec(asyncio.Transport, instance=True)
    mock_loop = _make_loop(tls_transport)

    with patch("asyncio.get_running_loop", autospec=True, return_value=mock_loop):
        await _imap_starttls(imap, ssl.create_default_context(), "127.0.0.1")

    imap.protocol.execute.assert_awaited_once()
    mock_loop.start_tls.assert_awaited_once()
    assert imap.protocol.transport is tls_transport
    imap.protocol.capability.assert_awaited_once()


@pytest.mark.asyncio
async def test_imap_starttls_requires_connected_protocol():
    imap = _make_imap()
    imap.protocol = None

    with pytest.raises(ConnectionError, match="IMAP protocol is not connected"):
        await _imap_starttls(imap, ssl.create_default_context(), "127.0.0.1")


@pytest.mark.asyncio
async def test_imap_starttls_requires_connected_transport():
    imap = _make_imap()
    imap.protocol.transport = None

    with pytest.raises(ConnectionError, match="IMAP transport is not connected"):
        await _imap_starttls(imap, ssl.create_default_context(), "127.0.0.1")


@pytest.mark.asyncio
async def test_imap_starttls_requires_upgraded_transport():
    imap = _make_imap()

    mock_loop = _make_loop(None)

    with patch("asyncio.get_running_loop", autospec=True, return_value=mock_loop):
        with pytest.raises(ConnectionError, match="IMAP STARTTLS did not return a transport"):
            await _imap_starttls(imap, ssl.create_default_context(), "127.0.0.1")

    mock_loop.start_tls.assert_awaited_once()
    imap.protocol.capability.assert_not_awaited()


@pytest.mark.asyncio
async def test_imap_starttls_requires_server_capability():
    imap = _make_imap(capabilities=("IMAP4rev1",))

    with pytest.raises(OSError, match="does not advertise STARTTLS"):
        await _imap_starttls(imap, ssl.create_default_context(), "127.0.0.1")


@pytest.mark.asyncio
async def test_imap_starttls_raises_when_command_fails():
    imap = _make_imap()
    imap.protocol.execute.return_value = Response("NO")

    with pytest.raises(OSError, match="STARTTLS command failed: NO"):
        await _imap_starttls(imap, ssl.create_default_context(), "127.0.0.1")


@pytest.mark.asyncio
async def test_connect_imap_uses_implicit_tls_when_use_ssl_true():
    server = EmailServer(
        user_name="user",
        password="secret",
        host="imap.example.com",
        port=993,
        use_ssl=True,
        verify_ssl=False,
    )
    mock_imap = _make_imap()

    with patch(
        "mcp_email_server.emails.classic.aioimaplib.IMAP4_SSL", autospec=True, return_value=mock_imap
    ) as mock_ssl:
        result = await EmailClient._connect_imap_server(server)

    assert result is mock_imap
    mock_ssl.assert_called_once()
    assert mock_ssl.call_args.kwargs["ssl_context"].verify_mode == ssl.CERT_NONE
    mock_imap.wait_hello_from_server.assert_awaited_once()


def test_imap_connect_uses_custom_context_when_verification_disabled():
    server = EmailServer(
        user_name="user",
        password="secret",
        host="imap.example.com",
        port=993,
        use_ssl=True,
        verify_ssl=False,
    )
    client = EmailClient(server)
    mock_imap_class = create_autospec(aioimaplib.IMAP4_SSL)
    client.imap_class = mock_imap_class

    result = client._imap_connect()

    ssl_context = mock_imap_class.call_args.kwargs["ssl_context"]
    assert result is mock_imap_class.return_value
    mock_imap_class.assert_called_once_with("imap.example.com", 993, ssl_context=ssl_context, timeout=60.0)
    assert isinstance(ssl_context, ssl.SSLContext)
    assert ssl_context.check_hostname is False
    assert ssl_context.verify_mode == ssl.CERT_NONE


@pytest.mark.asyncio
async def test_connect_imap_uses_plain_connection_without_starttls():
    server = EmailServer(
        user_name="user",
        password="secret",
        host="imap.example.com",
        port=143,
        use_ssl=False,
        start_ssl=False,
    )
    mock_imap = _make_imap()

    with patch("mcp_email_server.emails.classic.aioimaplib.IMAP4", autospec=True, return_value=mock_imap) as mock_plain:
        with patch("mcp_email_server.emails.classic._imap_starttls", autospec=True) as mock_starttls:
            result = await EmailClient._connect_imap_server(server)

    assert result is mock_imap
    mock_plain.assert_called_once_with("imap.example.com", 143, timeout=60.0)
    mock_starttls.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_imap_uses_starttls_when_configured():
    server = EmailServer(
        user_name="user",
        password="secret",
        host="127.0.0.1",
        port=1143,
        use_ssl=False,
        start_ssl=True,
        verify_ssl=False,
    )
    mock_imap = _make_imap()

    with patch("mcp_email_server.emails.classic.aioimaplib.IMAP4", autospec=True, return_value=mock_imap) as mock_plain:
        with patch("mcp_email_server.emails.classic._imap_starttls", autospec=True) as mock_starttls:
            result = await EmailClient._connect_imap_server(server)

    assert result is mock_imap
    mock_plain.assert_called_once_with("127.0.0.1", 1143, timeout=60.0)
    mock_starttls.assert_awaited_once()
    starttls_context = mock_starttls.await_args.args[1]
    assert starttls_context.verify_mode == ssl.CERT_NONE


@pytest.mark.asyncio
async def test_append_to_mailbox_uses_incoming_starttls_path():
    outgoing = EmailServer(user_name="user", password="secret", host="smtp.example.com", port=465)
    incoming = EmailServer(
        user_name="user",
        password="secret",
        host="127.0.0.1",
        port=1143,
        use_ssl=False,
        start_ssl=True,
        verify_ssl=False,
    )
    client = EmailClient(outgoing)
    mock_imap = _make_imap()
    mock_imap.login.return_value = Response("OK")
    mock_imap.select.return_value = ("OK", [])
    mock_imap.append.return_value = ("OK", [])

    with patch("mcp_email_server.emails.classic.aioimaplib.IMAP4", autospec=True, return_value=mock_imap):
        with patch("mcp_email_server.emails.classic._imap_starttls", autospec=True) as mock_starttls:
            result = await client.append_to_mailbox(MIMEText("message"), incoming, "Drafts")

    assert result == "unknown"
    mock_starttls.assert_awaited_once()
    mock_imap.append.assert_awaited_once()
