"""Server-enforced HTML paragraph style, signature and signature attachments."""

import tomllib
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mcp_email_server.adapters.mutations import ClassicMutationProvider
from mcp_email_server.application.mutations import MutationAccountSnapshot, SaveToMailboxCommand, SendCommand
from mcp_email_server.config import EmailServer, EmailSettings, Settings
from mcp_email_server.emails.classic import ClassicEmailHandler

STYLE = "margin:0;font-family:Calibri"
SIGNATURE = '<div id="sig">-- Team <img src="cid:logo.png"></div>'
PARENT = {
    "thread_index": "",
    "from": "a@example.com",
    "date": "today",
    "to": "b@example.com",
    "cc": "",
    "subject": "S",
    "body_text": "QUOTED-HISTORY-MARKER",
    "body_html": "",
}
PATHS = ["send", "save", "adapter_send", "adapter_save"]


def _server(host: str) -> EmailServer:
    return EmailServer(user_name="u", password="p", host=host, port=993, use_ssl=True)


def _settings(**extra: object) -> EmailSettings:
    return EmailSettings(
        account_name="acc",
        full_name="Test User",
        email_address="test@example.com",
        incoming=_server("imap.example.com"),
        outgoing=_server("smtp.example.com"),
        save_to_sent=False,
        **extra,  # pyright: ignore[reportArgumentType]
    )


async def _run(
    path: str,
    settings: EmailSettings,
    body: str,
    *,
    html: bool = True,
    attachments: list[str] | None = None,
    reply: bool = False,
) -> tuple[str, list[str] | None]:
    """Run one outgoing path; return the (body, attachments) that reach composition/SMTP."""
    handler = ClassicEmailHandler(settings)
    in_reply_to = "<parent@example.com>" if reply else None
    handler.incoming_client._lookup_parent = AsyncMock(return_value=dict(PARENT) if reply else None)  # type: ignore[method-assign]
    send = AsyncMock(return_value=None)
    compose = Mock(return_value=MIMEText("x"))
    append = AsyncMock(return_value="1")
    assert handler.outgoing_client is not None
    with (
        patch.object(handler.outgoing_client, "send_email", send),
        patch.object(handler.outgoing_client, "send_email_with_outcome", send),
        patch.object(handler.incoming_client, "compose_message", compose),
        patch.object(handler.incoming_client, "append_to_mailbox", append),
        patch.object(handler.incoming_client, "append_to_mailbox_with_outcome", append),
    ):
        to = ["r@example.com"]
        if path == "send":
            await handler.send_email(to, "S", body, html=html, attachments=attachments, in_reply_to=in_reply_to)
        elif path == "save":
            await handler.save_to_mailbox(to, "S", body, html=html, attachments=attachments, in_reply_to=in_reply_to)
        else:
            provider = ClassicMutationProvider(handler)
            account = MutationAccountSnapshot("acc", "legacy", (), (), False)
            common = {"html": html, "attachments": tuple(attachments or ()), "in_reply_to": in_reply_to}
            if path == "adapter_send":
                await provider.send(SendCommand("acc", tuple(to), "S", body, **common), account)
            else:
                await provider.save_to_mailbox(SaveToMailboxCommand("acc", tuple(to), "S", body, **common), account)
    call = send.call_args if path in ("send", "adapter_send") else compose.call_args
    return call.args[2], call.args[6]


@pytest.fixture
def sig_file(tmp_path: Path) -> Path:
    file = tmp_path / "sig.html"
    file.write_text(SIGNATURE, encoding="utf-8")
    return file


@pytest.fixture
def logo(tmp_path: Path) -> Path:
    file = tmp_path / "logo.png"
    file.write_bytes(b"\x89PNG")
    return file


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_paragraph_style_applied_to_p_without_style(path: str) -> None:
    body, _ = await _run(path, _settings(html_paragraph_style=STYLE), "<p>one</p><p>two</p>")
    assert body == f'<p style="{STYLE}">one</p><p style="{STYLE}">two</p>'


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_existing_paragraph_style_is_kept(path: str) -> None:
    src = '<P style="color:red">a</P><p class="x" STYLE="b:1">b</p><p class="y">c</p><pre>d</pre>'
    body, _ = await _run(path, _settings(html_paragraph_style=STYLE), src)
    assert '<P style="color:red">a</P>' in body
    assert '<p class="x" STYLE="b:1">b</p>' in body
    assert f'<p class="y" style="{STYLE}">c</p>' in body
    assert "<pre>d</pre>" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_plain_text_body_split_into_styled_paragraphs(path: str) -> None:
    body, _ = await _run(path, _settings(html_paragraph_style=STYLE), "first\nline\n\n\nsecond")
    assert body == f'<p style="{STYLE}">first<br>\nline</p>\n<p style="{STYLE}">second</p>'


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_paragraph_style_quote_is_escaped_in_attribute(path: str) -> None:
    body, _ = await _run(path, _settings(html_paragraph_style='font-family:"Calibri"'), "<p>x</p>")
    assert body == '<p style="font-family:&quot;Calibri&quot;">x</p>'


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_signature_precedes_quoted_history(path: str, sig_file: Path) -> None:
    settings = _settings(html_paragraph_style=STYLE, html_signature_path=str(sig_file))
    body, _ = await _run(path, settings, "<p>hi</p>", reply=True)
    assert body.index(SIGNATURE) < body.index("QUOTED-HISTORY-MARKER")
    assert body.index("<p ") < body.index(SIGNATURE)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_signature_without_paragraph_style_is_appended(path: str, sig_file: Path) -> None:
    body, _ = await _run(path, _settings(html_signature_path=str(sig_file)), "<p>hi</p>")
    assert body == f"<p>hi</p>\n{SIGNATURE}"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_signature_attachment_added_once(path: str, logo: Path) -> None:
    _, attachments = await _run(path, _settings(signature_attachments=[str(logo)]), "<p>x</p>")
    assert attachments == [str(logo)]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_signature_attachment_not_duplicated_for_same_path(path: str, logo: Path) -> None:
    settings = _settings(signature_attachments=[str(logo)])
    _, attachments = await _run(path, settings, "<p>x</p>", attachments=[str(logo)])
    assert attachments == [str(logo)]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_signature_attachment_not_duplicated_for_relative_path(
    path: str, logo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(logo.parent)
    settings = _settings(signature_attachments=[str(logo)])
    _, attachments = await _run(path, settings, "<p>x</p>", attachments=["./logo.png"])
    assert attachments == ["./logo.png"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_signature_attachment_keeps_caller_attachments(path: str, logo: Path, tmp_path: Path) -> None:
    other = tmp_path / "doc.txt"
    other.write_text("d")
    _, attachments = await _run(
        path, _settings(signature_attachments=[str(logo)]), "<p>x</p>", attachments=[str(other)]
    )
    assert attachments == [str(other), str(logo)]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_style_and_signature_unchanged_for_plain_text(path: str, sig_file: Path, logo: Path) -> None:
    settings = _settings(
        html_paragraph_style=STYLE, html_signature_path=str(sig_file), signature_attachments=[str(logo)]
    )
    body, attachments = await _run(path, settings, "a\n\nb", html=False, attachments=None, reply=True)
    assert body.startswith("a\n\nb")
    assert "sig" not in body
    assert attachments is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_account_without_style_fields_leaves_html_untouched(path: str) -> None:
    src = "<p>a</p>\nplain\n\ntext"
    body, attachments = await _run(path, _settings(), src, attachments=["/some/file.txt"])
    assert body == src
    assert attachments == ["/some/file.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_missing_signature_file_raises_before_send(path: str, tmp_path: Path) -> None:
    settings = _settings(html_signature_path=str(tmp_path / "absent.html"))
    with pytest.raises(ValueError, match="signature file is not readable"):
        await _run(path, settings, "<p>x</p>")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_missing_signature_attachment_raises_before_send(path: str, tmp_path: Path) -> None:
    settings = _settings(signature_attachments=[str(tmp_path / "absent.png")])
    with pytest.raises(FileNotFoundError, match="Signature attachment not found"):
        await _run(path, settings, "<p>x</p>")


def test_from_env_reads_style_and_signature_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "t@example.com",
        "MCP_EMAIL_SERVER_PASSWORD": "pw",
        "MCP_EMAIL_SERVER_IMAP_HOST": "imap.example.com",
        "MCP_EMAIL_SERVER_HTML_PARAGRAPH_STYLE": STYLE,
        "MCP_EMAIL_SERVER_HTML_SIGNATURE_PATH": "/etc/sig.html",
        "MCP_EMAIL_SERVER_SIGNATURE_ATTACHMENTS": " /a/logo.png , /b/x.png ,",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = EmailSettings.from_env()
    assert settings is not None
    assert settings.html_paragraph_style == STYLE
    assert settings.html_signature_path == "/etc/sig.html"
    assert settings.signature_attachments == ["/a/logo.png", "/b/x.png"]


def test_from_env_empty_style_and_signature_variables_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "t@example.com",
        "MCP_EMAIL_SERVER_PASSWORD": "pw",
        "MCP_EMAIL_SERVER_IMAP_HOST": "imap.example.com",
        "MCP_EMAIL_SERVER_HTML_PARAGRAPH_STYLE": "",
        "MCP_EMAIL_SERVER_HTML_SIGNATURE_PATH": "",
        "MCP_EMAIL_SERVER_SIGNATURE_ATTACHMENTS": "",
    }.items():
        monkeypatch.setenv(key, value)
    settings = EmailSettings.from_env()
    assert settings is not None
    assert settings.html_paragraph_style is None
    assert settings.html_signature_path is None
    assert settings.signature_attachments == []


def _toml_for(account: EmailSettings) -> str:
    settings = Settings()
    settings.add_email(account)
    return settings._to_toml()


def test_style_and_signature_fields_round_trip_through_toml() -> None:
    account = _settings(
        html_paragraph_style=STYLE, html_signature_path="/etc/sig.html", signature_attachments=["/a/logo.png"]
    )
    data = tomllib.loads(_toml_for(account))["emails"][0]
    restored = EmailSettings(**data)
    assert restored.html_paragraph_style == STYLE
    assert restored.html_signature_path == "/etc/sig.html"
    assert restored.signature_attachments == ["/a/logo.png"]


def test_style_and_signature_fields_absent_from_toml_when_unset() -> None:
    data = tomllib.loads(_toml_for(_settings()))["emails"][0]
    assert "html_paragraph_style" not in data
    assert "html_signature_path" not in data
