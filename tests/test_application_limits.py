from __future__ import annotations

from dataclasses import replace

import pytest

from mcp_email_server.application import limits


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("aaa", True),
        ("éé", True),
        ("ééa", False),
    ],
)
def test_controlled_string_limit_uses_utf8_bytes_at_limit_minus_one_limit_and_plus_one(
    value: str,
    valid: bool,
) -> None:
    if valid:
        assert (
            limits.validate_controlled_string(
                value,
                field_name="query",
                maximum_bytes=4,
            )
            == value
        )
    else:
        with pytest.raises(ValueError, match="exceeds 4 bytes"):
            limits.validate_controlled_string(
                value,
                field_name="query",
                maximum_bytes=4,
            )


@pytest.mark.parametrize("control", ["\x00", "\x1f", "\x7f"])
def test_controlled_string_rejects_c0_and_del(control: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        limits.validate_controlled_string(
            f"safe{control}unsafe",
            field_name="query",
            maximum_bytes=100,
        )


@pytest.mark.parametrize(
    ("payload", "valid"),
    [(b"aaa", True), (b"aaaa", True), (b"aaaaa", False)],
)
def test_serialized_result_limit_accepts_limit_minus_one_and_limit_only(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    valid: bool,
) -> None:
    monkeypatch.setattr(
        limits,
        "APPLICATION_LIMITS",
        replace(limits.APPLICATION_LIMITS, serialized_response_bytes=4),
    )

    if valid:
        limits.validate_serialized_result(payload)
    else:
        with pytest.raises(ValueError, match="serialized result exceeds 4 bytes"):
            limits.validate_serialized_result(payload)


@pytest.mark.parametrize(
    ("uid", "valid"),
    [("1", True), ("4294967294", True), ("4294967295", True), ("4294967296", False)],
)
def test_imap_uid_accepts_up_to_rfc_maximum_and_rejects_next(uid: str, valid: bool) -> None:
    if valid:
        assert limits.validate_imap_uid(uid) == uid
    else:
        with pytest.raises(ValueError, match="email_id exceeds the maximum IMAP UID"):
            limits.validate_imap_uid(uid)


@pytest.mark.parametrize("uid", ["0", "01", "-1", "+1", "1.0", " 1", "1\n", "", "\u0661"])
def test_imap_uid_rejects_non_canonical_form(uid: str) -> None:
    with pytest.raises(ValueError, match="email_id must be a canonical positive decimal IMAP UID"):
        limits.validate_imap_uid(uid)


def test_imap_uid_error_names_custom_field() -> None:
    with pytest.raises(ValueError, match=r"^uid must be a canonical"):
        limits.validate_imap_uid(1, field_name="uid")


@pytest.mark.parametrize("empty", ["", "   ", "\t \n"])
def test_controlled_string_rejects_blank_by_default(empty: str) -> None:
    with pytest.raises(ValueError, match=r"^query must not be empty$"):
        limits.validate_controlled_string(empty, field_name="query", maximum_bytes=10)


@pytest.mark.parametrize("empty", ["", "   "])
def test_controlled_string_allow_empty_accepts_blank(empty: str) -> None:
    assert limits.validate_controlled_string(empty, field_name="query", maximum_bytes=10, allow_empty=True) == empty


def test_controlled_string_rejects_non_string_with_field_name() -> None:
    with pytest.raises(ValueError, match=r"^query must be a string$"):
        limits.validate_controlled_string(5, field_name="query", maximum_bytes=10)


def test_controlled_string_error_messages_name_field() -> None:
    with pytest.raises(ValueError, match=r"^query must not contain control characters$"):
        limits.validate_controlled_string("a\x00", field_name="query", maximum_bytes=10)
    with pytest.raises(ValueError, match=r"^query exceeds 2 bytes$"):
        limits.validate_controlled_string("abc", field_name="query", maximum_bytes=2)


def test_optional_controlled_string_none_and_empty() -> None:
    assert limits.validate_optional_controlled_string(None, field_name="q", maximum_bytes=5) is None
    assert limits.validate_optional_controlled_string("", field_name="q", maximum_bytes=5) == ""
    with pytest.raises(ValueError, match=r"^q exceeds 5 bytes$"):
        limits.validate_optional_controlled_string("abcdef", field_name="q", maximum_bytes=5)
