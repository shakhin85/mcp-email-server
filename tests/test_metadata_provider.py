from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from mcp_email_server.application.metadata import MetadataProviderObservationError
from mcp_email_server.config import EmailServer
from mcp_email_server.emails.classic import (
    MAX_INDEXED_UID_WINDOW,
    MAX_METADATA_CANDIDATES,
    MAX_METADATA_HEADER_BYTES,
    MAX_METADATA_HEADER_FETCH_UIDS,
    MAX_METADATA_HEADER_TOTAL_BYTES,
    EmailClient,
    MailboxState,
    MetadataPayloadTooLargeError,
    MetadataQueryTooBroadError,
    _MetadataHeaderBudget,
)

NOW = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)


def _client() -> EmailClient:
    return EmailClient(
        EmailServer(
            user_name="user@example.test",
            password=SecretStr("secret"),
            host="imap.example.test",
            port=993,
        ),
        sender="User <user@example.test>",
    )


def _imap() -> AsyncMock:
    imap = AsyncMock()
    imap.login.return_value = MagicMock(result="OK", lines=[])
    imap.logout.return_value = ("BYE", [])
    handshake = asyncio.get_running_loop().create_future()
    handshake.set_result(None)
    imap._client_task = handshake
    return imap


def _connect(client: EmailClient, imap: AsyncMock) -> None:
    """Route the client's connection factory to a scripted server (real greeting/prepare path)."""
    client.imap_class = lambda *args, **kwargs: imap


def _raw_headers(subject: str, sender: str = "sender@example.test") -> bytes:
    return (
        f"From: {sender}\r\nTo: user@example.test\r\nSubject: {subject}\r\n"
        "Date: Tue, 22 Jul 2026 04:00:00 +0000\r\n\r\n"
    ).encode()


def _fetching_server(
    *,
    dates: dict[str, str] | None = None,
    stray_dates: dict[str, str] | None = None,
    headers: dict[str, tuple[str, list[str]]] | None = None,
    senders: dict[str, str] | None = None,
):
    """UID FETCH responder that answers with real IMAP wire shapes for the UIDs requested."""

    async def uid(command: str, uid_list: str, item: str):
        assert command == "fetch"
        requested = uid_list.split(",")
        lines: list[object] = []
        if "INTERNALDATE" in item:
            for u, value in {
                **{k: v for k, v in (dates or {}).items() if k in requested},
                **(stray_dates or {}),
            }.items():
                lines.append(f'{u} FETCH (UID {u} INTERNALDATE "{value}")'.encode())
        elif "HEADER.FIELDS" in item:
            for u in requested:
                if u in (senders or {}):
                    lines.append(f"{u} FETCH (UID {u} BODY[HEADER.FIELDS (FROM)] {{40}}".encode())
                    lines += [bytearray(f"From: {senders[u]}\r\n\r\n".encode()), b")"]
        else:
            for u in requested:
                if u in (headers or {}):
                    subject, flags = headers[u]
                    flag_part = f"FLAGS ({' '.join(flags)}) " if "FLAGS" in item else ""
                    lines.append(f"{u} FETCH (UID {u} {flag_part}BODY[HEADER] {{120}}".encode())
                    lines += [bytearray(_raw_headers(subject)), b")"]
        return "OK", lines

    return uid


INTERNAL_DATE = "22-Jul-2026 04:00:00 +0000"


def test_mailbox_status_requires_uidvalidity_uidnext_and_message_count() -> None:
    state = EmailClient._parse_mailbox_state(("OK", [b"INBOX (MESSAGES 12 UIDNEXT 20 UIDVALIDITY 777)"]))
    assert state == MailboxState(uidvalidity=777, uidnext=20, message_count=12)

    with pytest.raises(RuntimeError, match="required bounded state"):
        EmailClient._parse_mailbox_state(("OK", [b"INBOX (MESSAGES 12 UIDNEXT 20)"]))


@pytest.mark.asyncio
async def test_snapshot_is_complete_only_for_exact_bounded_epoch_and_uses_peek_headers() -> None:
    client = _client()
    imap = _imap()
    imap.list.return_value = ("OK", [b'(\\HasNoChildren \\Inbox) "/" "INBOX"'])
    imap.status.return_value = ("OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 4 MESSAGES 3)"])
    imap.select.return_value = ("OK", [b"3"])
    imap.uid_search.return_value = ("OK", [b"1 2 3"])
    imap.uid.side_effect = _fetching_server(
        dates={str(uid): INTERNAL_DATE for uid in range(1, 4)},
        headers={str(uid): (f"Subject {uid}", ["\\Seen"] if uid == 3 else []) for uid in range(1, 4)},
    )
    _connect(client, imap)

    snapshot = await client.get_mailbox_metadata_snapshot("INBOX")

    assert snapshot.complete is True
    assert snapshot.state == MailboxState(77, 4, 3)
    assert snapshot.mailbox.name == "INBOX"
    assert snapshot.mailbox.delimiter == "/"
    assert [email["email_id"] for email in snapshot.emails] == ["3", "2", "1"]
    assert [email["subject"] for email in snapshot.emails] == ["Subject 3", "Subject 2", "Subject 1"]
    assert snapshot.emails[0]["_internal_date"] == NOW
    assert snapshot.emails[0]["_flags"] == ["\\Seen"]
    assert snapshot.emails[1]["_flags"] == []
    header_fetches = [c.args for c in imap.uid.await_args_list if "HEADER" in c.args[2]]
    assert [uid_list for _, uid_list, _ in header_fetches] == ["3,2,1"]
    assert all("BODY.PEEK[HEADER]" in item and "BODY[HEADER]" not in item for _, _, item in header_fetches)
    assert imap.status.await_count == 2


@pytest.mark.asyncio
async def test_snapshot_state_change_cannot_claim_complete_coverage() -> None:
    client = _client()
    imap = _imap()
    imap.list.return_value = ("OK", [])
    imap.status.side_effect = [
        ("OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 2 MESSAGES 1)"]),
        ("OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 3 MESSAGES 1)"]),
    ]
    imap.select.return_value = ("OK", [])
    imap.uid_search.return_value = ("OK", [b"2"])
    imap.uid.side_effect = _fetching_server(dates={"2": INTERNAL_DATE}, headers={"2": ("Replacement", [])})
    _connect(client, imap)

    snapshot = await client.get_mailbox_metadata_snapshot()

    assert snapshot.complete is False
    assert snapshot.state == MailboxState(77, 3, 1)
    assert [email["email_id"] for email in snapshot.emails] == ["2"]


@pytest.mark.asyncio
async def test_snapshot_discards_rows_when_uidvalidity_changes_during_refresh() -> None:
    client = _client()
    imap = _imap()
    imap.list.return_value = ("OK", [])
    imap.status.side_effect = [
        ("OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 2 MESSAGES 1)"]),
        ("OK", [b"INBOX (UIDVALIDITY 78 UIDNEXT 2 MESSAGES 1)"]),
    ]
    imap.select.return_value = ("OK", [])
    imap.uid_search.return_value = ("OK", [b"1"])
    imap.uid.side_effect = _fetching_server(dates={"1": INTERNAL_DATE}, headers={"1": ("Old epoch", [])})
    _connect(client, imap)

    snapshot = await client.get_mailbox_metadata_snapshot()

    assert snapshot.complete is False
    assert snapshot.state == MailboxState(78, 2, 1)
    assert snapshot.emails == ()


@pytest.mark.asyncio
async def test_header_snapshot_fetches_flags_without_marking_messages_seen() -> None:
    client = _client()
    imap = _imap()
    raw_headers = b"From: sender@example.test\r\nTo: user@example.test\r\nSubject: Test\r\nDate: Tue, 22 Jul 2026 04:00:00 +0000\r\n\r\n"
    imap.uid.return_value = (
        "OK",
        [
            b"1 FETCH (UID 9 FLAGS (\\Seen \\Flagged) BODY[HEADER] {120}",
            bytearray(raw_headers),
            b")",
        ],
    )

    result = await client._batch_fetch_headers(imap, ["9"], include_flags=True)

    assert result["9"]["_flags"] == ["\\Seen", "\\Flagged"]
    imap.uid.assert_awaited_once_with("fetch", "9", "(FLAGS BODY.PEEK[HEADER]<0.65537>)")


@pytest.mark.asyncio
async def test_header_fetch_uses_partial_requests_with_wire_bounded_uid_chunks() -> None:
    client = _client()
    imap = _imap()
    raw_headers = b"From: sender@example.test\r\nSubject: Test\r\n\r\n"

    async def fetch(_command: str, uid_list: str, fetch_item: str):
        assert fetch_item == "BODY.PEEK[HEADER]<0.65537>"
        requested = uid_list.split(",")
        assert len(requested) <= MAX_METADATA_HEADER_FETCH_UIDS
        data: list[bytes | bytearray] = []
        for uid in requested:
            data.extend([
                f"1 FETCH (UID {uid} BODY[HEADER]<0> {{{len(raw_headers)}}}".encode(),
                bytearray(raw_headers),
                b")",
            ])
        return "OK", data

    imap.uid.side_effect = fetch
    email_ids = [str(uid) for uid in range(1, MAX_METADATA_HEADER_FETCH_UIDS + 2)]

    result = await client._batch_fetch_headers(imap, email_ids)

    assert set(result) == set(email_ids)
    assert imap.uid.await_count == 2


@pytest.mark.asyncio
async def test_header_fetch_rejects_provider_payload_above_per_message_budget() -> None:
    client = _client()
    imap = _imap()
    imap.uid.return_value = (
        "OK",
        [
            b"1 FETCH (UID 9 BODY[HEADER] {65537}",
            bytearray(b"x" * (MAX_METADATA_HEADER_BYTES + 1)),
            b")",
        ],
    )

    with pytest.raises(MetadataPayloadTooLargeError, match="provider_payload_too_large"):
        await client._batch_fetch_headers(imap, ["9"])


@pytest.mark.asyncio
async def test_header_fetch_rejects_provider_payload_above_batch_budget() -> None:
    client = _client()
    imap = _imap()
    message_count = MAX_METADATA_HEADER_TOTAL_BYTES // MAX_METADATA_HEADER_BYTES + 1
    response_items: list[bytes | bytearray] = []
    for uid in range(1, message_count + 1):
        response_items.extend([
            f"1 FETCH (UID {uid} BODY[HEADER] {{{MAX_METADATA_HEADER_BYTES}}}".encode(),
            bytearray(b"x" * MAX_METADATA_HEADER_BYTES),
            b")",
        ])
    imap.uid.return_value = ("OK", response_items)

    with pytest.raises(MetadataPayloadTooLargeError, match="header query exceeds 4 MiB"):
        await client._batch_fetch_headers(imap, [str(uid) for uid in range(1, message_count + 1)])


@pytest.mark.asyncio
async def test_sender_and_page_fetches_share_one_logical_query_budget() -> None:
    client = _client()
    imap = _imap()
    budget = _MetadataHeaderBudget()
    sender_items: list[bytes | bytearray] = []
    sender_ids = [str(uid) for uid in range(1, MAX_METADATA_HEADER_FETCH_UIDS + 1)]
    for uid in sender_ids:
        sender_items.extend([
            f"1 FETCH (UID {uid} BODY[HEADER.FIELDS (FROM)] {{{MAX_METADATA_HEADER_BYTES}}}".encode(),
            bytearray(b"x" * MAX_METADATA_HEADER_BYTES),
            b")",
        ])
    imap.uid.return_value = ("OK", sender_items)
    await client._batch_fetch_senders(imap, sender_ids, header_budget=budget)

    imap.uid.return_value = (
        "OK",
        [
            b"1 FETCH (UID 64 BODY[HEADER] {65536}",
            bytearray(b"x" * MAX_METADATA_HEADER_BYTES),
            b")",
            b"2 FETCH (UID 65 BODY[HEADER] {1}",
            bytearray(b"x"),
            b")",
        ],
    )
    with pytest.raises(MetadataPayloadTooLargeError, match="header query exceeds 4 MiB"):
        await client._batch_fetch_headers(imap, ["64", "65"], header_budget=budget)


@pytest.mark.asyncio
async def test_sender_allowlist_header_fetch_uses_same_payload_budget() -> None:
    client = _client()
    imap = _imap()
    imap.uid.return_value = (
        "OK",
        [
            b"1 FETCH (UID 9 BODY[HEADER.FIELDS (FROM)] {65537}",
            bytearray(b"x" * (MAX_METADATA_HEADER_BYTES + 1)),
            b")",
        ],
    )

    with pytest.raises(MetadataPayloadTooLargeError, match="metadata header exceeds 64 KiB"):
        await client._batch_fetch_senders(imap, ["9"])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["NO", "BAD", "BYE", "UNEXPECTED"])
async def test_internaldate_fetch_requires_ok_status(status: str) -> None:
    client = _client()
    imap = _imap()
    imap.uid.return_value = (status, [b"provider detail must not be exposed"])

    with pytest.raises(RuntimeError, match="FETCH INTERNALDATE for 1 UIDs failed") as exc_info:
        await client._fetch_dates_chunk(imap, ["1"], 1, 1)
    assert "provider detail" not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["NO", "BAD", "BYE", "UNEXPECTED"])
async def test_provider_fallback_rejects_failed_search_instead_of_false_empty(status: str) -> None:
    client = _client()
    imap = _imap()
    imap.select.return_value = ("OK", [])
    imap.uid_search.return_value = (status, [b"provider detail must not be exposed"])
    _connect(client, imap)
    with pytest.raises(RuntimeError, match="SEARCH mailbox INBOX failed") as exc_info:
        await client.get_emails_metadata()
    assert "provider detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_snapshot_search_failure_is_not_interpreted_as_empty_coverage() -> None:
    client = _client()
    imap = _imap()
    imap.list.return_value = ("OK", [])
    imap.status.return_value = ("OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 2 MESSAGES 1)"])
    imap.select.return_value = ("OK", [])
    imap.uid_search.return_value = ("NO", [])
    _connect(client, imap)
    with pytest.raises(RuntimeError, match="SEARCH mailbox INBOX failed"):
        await client.get_mailbox_metadata_snapshot()


@pytest.mark.asyncio
async def test_provider_fallback_rejects_incomplete_sender_allowlist_evidence() -> None:
    client = _client()
    imap = _imap()
    imap.select.return_value = ("OK", [])
    imap.uid_search.return_value = ("OK", [b"1 2"])
    imap.uid.side_effect = _fetching_server(
        senders={"1": "allowed@example.test"},
        dates={"1": INTERNAL_DATE, "2": INTERNAL_DATE},
    )
    _connect(client, imap)
    with pytest.raises(RuntimeError, match="incomplete sender metadata"):
        await client.get_emails_metadata(allowed_senders=["allowed@example.test"])
    assert all("INTERNALDATE" not in c.args[2] for c in imap.uid.await_args_list)


@pytest.mark.asyncio
async def test_snapshot_rejects_missing_internaldate_instead_of_claiming_coverage() -> None:
    client = _client()
    imap = _imap()
    imap.list.return_value = ("OK", [])
    imap.status.return_value = ("OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 2 MESSAGES 1)"])
    imap.select.return_value = ("OK", [])
    imap.uid_search.return_value = ("OK", [b"1"])
    imap.uid.side_effect = _fetching_server(dates={}, headers={"1": ("No date evidence", [])})
    _connect(client, imap)

    with pytest.raises(MetadataProviderObservationError, match="incomplete INTERNALDATE"):
        await client.get_mailbox_metadata_snapshot()


@pytest.mark.asyncio
async def test_snapshot_rejects_wrong_internaldate_uid_set_instead_of_claiming_coverage() -> None:
    client = _client()
    imap = _imap()
    imap.list.return_value = ("OK", [])
    imap.status.return_value = ("OK", [b"INBOX (UIDVALIDITY 77 UIDNEXT 2 MESSAGES 1)"])
    imap.select.return_value = ("OK", [])
    imap.uid_search.return_value = ("OK", [b"1"])
    imap.uid.side_effect = _fetching_server(
        stray_dates={"999": INTERNAL_DATE}, headers={"1": ("Wrong date evidence", [])}
    )
    _connect(client, imap)

    with pytest.raises(MetadataProviderObservationError, match="invalid INTERNALDATE"):
        await client.get_mailbox_metadata_snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "search_payload",
    [b"1:*", b"1,2", b"0", b"01", b"4294967296", b"1 1", b"\xff"],
)
async def test_provider_fallback_rejects_noncanonical_search_uids_before_fetch_work(
    search_payload: bytes,
) -> None:
    client = _client()
    imap = _imap()
    imap.select.return_value = ("OK", [])
    imap.uid_search.return_value = ("OK", [search_payload])
    _connect(client, imap)
    with pytest.raises(MetadataProviderObservationError, match="invalid UID search results"):
        await client.get_emails_metadata()

    imap.uid.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_fallback_rejects_same_count_wrong_internaldate_uid_set() -> None:
    client = _client()
    imap = _imap()
    imap.select.return_value = ("OK", [])
    imap.uid_search.return_value = ("OK", [b"1 2"])
    imap.uid.side_effect = _fetching_server(
        dates={"1": INTERNAL_DATE},
        stray_dates={"999": INTERNAL_DATE},
        headers={"1": ("One", []), "2": ("Two", [])},
    )
    _connect(client, imap)
    with pytest.raises(MetadataProviderObservationError, match="INTERNALDATE"):
        await client.get_emails_metadata()

    assert all("HEADER" not in c.args[2] for c in imap.uid.await_args_list)


@pytest.mark.asyncio
async def test_internaldate_fetch_rejects_unrequested_response_uid() -> None:
    client = _client()
    imap = _imap()
    imap.uid.return_value = (
        "OK",
        [b'1 FETCH (UID 999 INTERNALDATE "22-Jul-2026 04:00:00 +0000")'],
    )

    with pytest.raises(MetadataProviderObservationError, match="invalid INTERNALDATE"):
        await client._fetch_dates_chunk(imap, ["1"], 1, 1)


@pytest.mark.asyncio
async def test_provider_fallback_rejects_more_than_candidate_ceiling_before_fetch_work() -> None:
    client = _client()
    imap = _imap()
    imap.select.return_value = ("OK", [])
    imap.uid_search.return_value = (
        "OK",
        [b" ".join(str(uid).encode() for uid in range(1, MAX_METADATA_CANDIDATES + 2))],
    )
    _connect(client, imap)
    with pytest.raises(MetadataQueryTooBroadError, match="query_too_broad"):
        await client.get_emails_metadata()

    imap.uid.assert_not_awaited()
    imap.logout.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_fallback_rejects_incomplete_page_instead_of_returning_short_exact_result() -> None:
    client = _client()
    imap = _imap()
    imap.select.return_value = ("OK", [])
    imap.uid_search.return_value = ("OK", [b"1"])
    imap.uid.side_effect = _fetching_server(dates={"1": INTERNAL_DATE}, headers={})
    _connect(client, imap)
    with pytest.raises(RuntimeError, match="incomplete metadata page"):
        await client.get_emails_metadata()


@pytest.mark.asyncio
async def test_snapshot_larger_than_recent_window_is_partial_without_absence_deletion_claim() -> None:
    client = _client()
    imap = _imap()
    count = MAX_INDEXED_UID_WINDOW + 1
    imap.list.return_value = ("OK", [])
    imap.status.return_value = (
        "OK",
        [f"INBOX (UIDVALIDITY 77 UIDNEXT {count + 1} MESSAGES {count})".encode()],
    )
    imap.select.return_value = ("OK", [])
    imap.uid_search.return_value = (
        "OK",
        [b" ".join(str(uid).encode() for uid in range(1, count + 1))],
    )
    selected = [str(uid) for uid in range(count, 1, -1)]
    imap.uid.side_effect = _fetching_server(
        dates=dict.fromkeys(selected, INTERNAL_DATE),
        headers={uid: (f"Subject {uid}", []) for uid in selected},
    )
    _connect(client, imap)

    snapshot = await client.get_mailbox_metadata_snapshot("INBOX")

    assert snapshot.complete is False
    assert len(snapshot.emails) == MAX_INDEXED_UID_WINDOW
    assert snapshot.emails[0]["email_id"] == str(count)
    assert snapshot.emails[-1]["email_id"] == "2"


def test_header_parse_error_does_not_log_provider_controlled_exception() -> None:
    client = _client()
    with (
        patch("mcp_email_server.emails.classic.BytesParser", side_effect=RuntimeError("sensitive provider text")),
        patch("mcp_email_server.emails.classic.logger.error") as error_log,
    ):
        result = client._parse_headers("9", b"provider-controlled headers")

    assert result is None
    error_log.assert_called_once_with("Error parsing email headers")
