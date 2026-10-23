"""Server-enforced HTML body style and signature from the account configuration."""

from __future__ import annotations

import re
from html import escape as escape_html
from pathlib import Path

from mcp_email_server.config import EmailSettings

_P_TAG = re.compile(r"<p(?P<attrs>\s[^>]*)?>", re.IGNORECASE)
_STYLE_ATTR = re.compile(r"\sstyle\s*=", re.IGNORECASE)
_BLANK_LINES = re.compile(r"\n\s*\n")


def style_paragraphs(body: str, style: str) -> str:
    """Add ``style`` to every ``<p>`` lacking one; wrap plain text into styled paragraphs."""
    attr = f' style="{escape_html(style, quote=True)}"'
    if not _P_TAG.search(body):
        chunks = [chunk.strip() for chunk in _BLANK_LINES.split(body) if chunk.strip()]
        return "\n".join(f"<p{attr}>{chunk.replace(chr(10), '<br>' + chr(10))}</p>" for chunk in chunks)

    def _add(match: re.Match[str]) -> str:
        attrs = match.group("attrs") or ""
        if _STYLE_ATTR.search(attrs):
            return match.group(0)
        return f"<p{attrs}{attr}>"

    return _P_TAG.sub(_add, body)


def _read_signature(path: str) -> str:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"HTML signature file is not readable: {path}: {exc}") from exc


def _merge_attachments(configured: list[str], attachments: list[str] | None) -> list[str]:
    merged = list(attachments or [])
    known = {Path(item).expanduser().resolve() for item in merged}
    for item in configured:
        path = Path(item).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Signature attachment not found: {item}")
        if path.resolve() not in known:
            known.add(path.resolve())
            merged.append(item)
    return merged


def apply_account_format(
    settings: EmailSettings, body: str, html: bool, attachments: list[str] | None
) -> tuple[str, list[str] | None]:
    """Return ``(body, attachments)`` with the account's style, signature and logo applied.

    Runs before the quoted history is appended, so the signature precedes it.
    ``html=False`` and accounts without the fields pass through unchanged.
    """
    if not html:
        return body, attachments
    if settings.html_paragraph_style:
        body = style_paragraphs(body, settings.html_paragraph_style)
    if settings.html_signature_path:
        body = f"{body}\n{_read_signature(settings.html_signature_path)}"
    if settings.signature_attachments:
        attachments = _merge_attachments(settings.signature_attachments, attachments)
    return body, attachments
