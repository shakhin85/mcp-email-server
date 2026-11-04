"""A reply finds its parent in a localized Sent folder; a missing parent is reported."""

from email.mime.text import MIMEText
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mcp_email_server.adapters.mutations import ClassicMutationProvider
from mcp_email_server.app import save_to_mailbox, send_email
from mcp_email_server.application.mutations import (
    AppendMutationOutcome,
    DeliveryMutationOutcome,
    SaveToMailboxCommand,
    SendCommand,
    SendMutationOutcome,
    SentCopyMutationOutcome,
    TargetMutationOutcome,
)
from mcp_email_server.config import EmailServer, EmailSettings
from mcp_email_server.emails.classic import ClassicEmailHandler, _quote_mailbox

PARENT_ID = "<parent@example.test>"
RAW_PARENT = (
    b"From: Alice <alice@example.test>\r\n"
    b"Subject: Original\r\n"
    b"Date: Tue, 03 Nov 2026 17:40:00 +0300\r\n"
    b"Thread-Index: AdX0parent==\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Parent body\r\n"
)
SENT_FOLDER = "Отправленные"


def _server() -> EmailServer:
    return EmailServer(user_name="user", password="secret", host="mail.example.test", port=993, use_ssl=True)


def _handler() -> ClassicEmailHandler:
    return ClassicEmailHandler(
        EmailSettings(
            account_name="primary",
            full_name="User",
            email_address="user@example.test",
            incoming=_server(),
            outgoing=_server(),
        )
    )


def _imap(*, parent_folder: str | None) -> AsyncMock:
    """IMAP server with a localized \\Sent folder; only that folder and INBOX exist."""
    selected: dict[str, str] = {}
    existing = {_quote_mailbox("INBOX", utf8=False), _quote_mailbox(SENT_FOLDER, utf8=False)}

    async def select(mailbox: str) -> tuple[str, list[bytes]]:
        if mailbox not in existing:
            return ("NO", [b"no such mailbox"])
        selected["mailbox"] = mailbox
        return ("OK", [b"1"])

    async def uid_search(*_args: object, **_kwargs: object) -> tuple[str, list[bytes]]:
        found = parent_folder is not None and selected.get("mailbox") == _quote_mailbox(parent_folder, utf8=False)
        return ("OK", [b"7" if found else b""])

    imap = AsyncMock()
    imap.select = AsyncMock(side_effect=select)
    imap.uid_search = AsyncMock(side_effect=uid_search)
    imap.list = AsyncMock(
        return_value=("OK", [b'(\\Sent \\HasNoChildren) "/" "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"', b"LIST completed"])
    )
    imap.protocol.capabilities = set()
    return imap


async def _resolve(handler: ClassicEmailHandler, imap: AsyncMock):
    client = handler.incoming_client
    with (
        patch.object(client, "_connect_imap", AsyncMock(return_value=imap)),
        patch.object(client, "_fetch_email_with_formats", AsyncMock(return_value=[RAW_PARENT])),
        patch.object(client, "_extract_raw_email", Mock(return_value=RAW_PARENT)),
        patch("mcp_email_server.emails.classic._imap_login", AsyncMock()),
    ):
        return await handler.resolve_reply("My reply", False, PARENT_ID, True)


@pytest.mark.asyncio
async def test_parent_in_localized_sent_folder_is_quoted_and_threaded() -> None:
    reply = await _resolve(_handler(), _imap(parent_folder=SENT_FOLDER))

    assert "Parent body" in reply[0]
    assert reply[1] == "AdX0parent=="


@pytest.mark.asyncio
async def test_parent_absent_everywhere_is_marked_missing() -> None:
    reply = await _resolve(_handler(), _imap(parent_folder=None))

    assert reply.body == "My reply"
    assert reply.thread_index is None
    assert reply.parent_missing is True


@pytest.mark.asyncio
async def test_message_without_in_reply_to_is_not_marked_missing() -> None:
    reply = await _handler().resolve_reply("Body", False, None, True)

    assert reply.parent_missing is False


@pytest.mark.asyncio
async def test_provider_marks_saved_draft_when_parent_missing() -> None:
    handler = _handler()
    client = handler.incoming_client
    with (
        patch.object(client, "_lookup_parent", AsyncMock(return_value=None)),
        patch.object(
            client,
            "append_to_mailbox_with_outcome",
            AsyncMock(return_value=AppendMutationOutcome("succeeded", "<id>", uid="5", mailbox="Drafts")),
        ),
    ):
        outcome = await ClassicMutationProvider(handler).save_to_mailbox(
            SaveToMailboxCommand("primary", ("to@example.test",), "Re: x", "Body", in_reply_to=PARENT_ID),
            Mock(),
        )

    assert outcome.parent_missing is True


@pytest.mark.asyncio
async def test_provider_marks_delivery_when_parent_missing() -> None:
    handler = _handler()
    delivery = DeliveryMutationOutcome((TargetMutationOutcome("to@example.test", "succeeded"),), MIMEText("body"))
    with (
        patch.object(handler.incoming_client, "_lookup_parent", AsyncMock(return_value=None)),
        patch.object(handler.outgoing_client, "send_email_with_outcome", AsyncMock(return_value=delivery)),
    ):
        outcome = await ClassicMutationProvider(handler).send(
            SendCommand("primary", ("to@example.test",), "Re: x", "Body", in_reply_to=PARENT_ID),
            Mock(),
        )

    assert outcome.parent_missing is True


@pytest.mark.asyncio
async def test_save_to_mailbox_tool_warns_about_missing_parent() -> None:
    outcome = AppendMutationOutcome("succeeded", "<id>", uid="5", mailbox="Drafts", parent_missing=True)
    with patch("mcp_email_server.app.save_to_mailbox_command", AsyncMock(return_value=outcome)):
        result = await save_to_mailbox("primary", ["to@example.test"], "Re: x", "Body", in_reply_to=PARENT_ID)

    assert "saved to 'Drafts' successfully" in result
    assert "warning: reply parent not found" in result


@pytest.mark.asyncio
async def test_send_email_tool_warns_about_missing_parent() -> None:
    outcome = SendMutationOutcome(
        (TargetMutationOutcome("to@example.test", "succeeded"),),
        SentCopyMutationOutcome("succeeded", "Sent"),
        parent_missing=True,
    )
    with patch("mcp_email_server.app.send_email_command", AsyncMock(return_value=outcome)):
        result = await send_email("primary", ["to@example.test"], "Re: x", "Body", in_reply_to=PARENT_ID)

    assert "Email sent successfully" in result
    assert "warning: reply parent not found" in result


@pytest.mark.asyncio
async def test_tools_stay_silent_when_parent_found() -> None:
    saved = AppendMutationOutcome("succeeded", "<id>", uid="5", mailbox="Drafts")
    with patch("mcp_email_server.app.save_to_mailbox_command", AsyncMock(return_value=saved)):
        result = await save_to_mailbox("primary", ["to@example.test"], "Re: x", "Body", in_reply_to=PARENT_ID)

    assert "warning" not in result
