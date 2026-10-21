"""Guards against binary PDF data contaminating textual evidence stores."""

from __future__ import annotations

import re


_PDF_SIGNATURE = re.compile(r"%PDF-\d(?:\.\d)?", re.IGNORECASE)
_PDF_OBJECT_MARKERS = re.compile(r"(?:^|\n)\s*(?:endstream|xref|endobj)\b", re.IGNORECASE)


def looks_like_pdf_corruption(value: str | bytes | None) -> bool:
    """Return true when *value* looks like raw PDF/object data.

    Ordinary prose can mention a data stream or an xref concept.  We therefore
    require a PDF signature, binary NULs, or multiple object markers rather
    than rejecting every occurrence of the word ``stream``.
    """

    if value is None:
        return False
    raw = value if isinstance(value, bytes) else value.encode("utf-8", errors="ignore")
    if b"%PDF-" in raw[:1024] or b"\x00" in raw:
        return True
    text = value.decode("latin-1", errors="ignore") if isinstance(value, bytes) else value
    markers = len(_PDF_OBJECT_MARKERS.findall(text))
    return markers >= 2 or bool(re.search(r"\bobj\s*<</", text, re.IGNORECASE))


def validate_extracted_text(value: str | bytes | None) -> str:
    """Validate and return parsed text suitable for evidence fields.

    ``ValueError`` is intentional: callers must record ``EXTRACTION_FAILED``
    rather than silently turning corrupt bytes into a non-disclosure.
    """

    if isinstance(value, bytes):
        raise ValueError("evidence text must be decoded text, not raw bytes")
    text = str(value or "")
    if looks_like_pdf_corruption(text):
        raise ValueError("raw PDF/object stream detected in extracted text")
    # A high control-character ratio is another reliable indication that a
    # decoder returned compressed bytes instead of page text.
    if text and sum(ord(char) < 32 and char not in "\n\r\t" for char in text) / len(text) > 0.01:
        raise ValueError("binary-like control characters detected in extracted text")
    return text


def safe_text(value: str | bytes | None) -> tuple[str, str]:
    """Return ``(text, status)`` for ingestion pipelines that do not raise."""

    if value is None:
        return "", "EXTRACTION_FAILED"
    try:
        return validate_extracted_text(value), "VERIFIED"
    except ValueError:
        return "", "EXTRACTION_FAILED"


__all__ = ["looks_like_pdf_corruption", "validate_extracted_text", "safe_text"]
