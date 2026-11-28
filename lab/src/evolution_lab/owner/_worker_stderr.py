"""Best-effort, bounded exception records for installed workers."""
from __future__ import annotations

import sys
import traceback

from evolution.tree.storage.redaction import safe_record

from .models import _WORKER_EXCEPTION_CLASS, _WORKER_EXCEPTION_CODE


_MAX_EXCEPTION_BYTES = 65536
_TRUNCATED = "\n[traceback truncated]\n"


def exception_record(error: BaseException) -> dict:
    """Bounded code identifiers only; never format a worker's private message."""
    try:
        cls = type(error)
        name = cls.__module__ + "." + cls.__qualname__
        code = getattr(error, "code", None) if cls.__module__.startswith("evolution.") else None
        return {"class": name if _WORKER_EXCEPTION_CLASS.fullmatch(name) else "unknown",
                "code": code if isinstance(code, str) and _WORKER_EXCEPTION_CODE.fullmatch(code) else None}
    except BaseException:
        return {"class": "unknown", "code": None}


def exception_redactions(environ, redactions=()) -> tuple[str, ...]:
    """Cover every supplied value, including short credentials and target values.

    Core's journal redactions apply a length floor. Exception messages can
    contain any supplied credential, so this additional log path has no floor.
    """
    return tuple(dict.fromkeys((*redactions, *(value for value in environ.values() if value))))


def _mask(text: str, redactions: tuple[str, ...], marker: str) -> str:
    """Replace the union of matches in the original text, including overlaps.

    Searching after replacement loses suffixes shared with a second secret.
    Advance by one character, not the match length, to cover self-overlaps too.
    The marker contains no character used by any secret, so replacements cannot
    expose a secret through their own spelling or create one across a boundary.
    """
    spans = []
    for secret in redactions:
        start = text.find(secret)
        while start >= 0:
            spans.append((start, start + len(secret)))
            start = text.find(secret, start + 1)
    parts, end = [], 0
    for start, stop in sorted(spans):
        if stop <= end:
            continue
        if start >= end:
            parts.extend((text[end:start], marker))
        end = stop
    parts.append(text[end:])
    return "".join(parts)


def _redacted(text: str, redactions: tuple[str, ...], marker: str) -> str:
    # Cover original occurrences before Core can alter or split a secret.
    masked = _mask(text, redactions, marker)
    private_masked = _mask(safe_record(masked, ()), redactions, marker)
    # Mask Core's own privacy marker before passing run redactions, too.
    result = _mask(safe_record(private_masked, redactions), redactions, marker)
    # Masking 'analysis' can break <analysis>; Core privacy can also split a
    # secret containing such markup. Only emit when both orders agree, keeping
    # the original union and Core's privacy on intact text. Otherwise fail closed.
    if private_masked != _mask(safe_record(text, ()), redactions, marker):
        raise ValueError("privacy and secret redaction conflict")
    return result


def write_exception(error: BaseException, redactions: tuple[str, ...] | None) -> None:
    """None means redactions are not ready; an empty tuple is a ready set."""
    try:
        text = (type(error).__name__ + "\n" if redactions is None else
                "".join(traceback.format_exception(type(error), error, error.__traceback__)))
        redactions = tuple(dict.fromkeys(value for value in redactions or () if value))
        # A one-character separator outside the secret alphabet also protects
        # very short values and secrets contained in the usual [REDACTED] marker.
        marker = "█"
        while any(marker in secret for secret in redactions):
            codepoint = ord(marker) + 1
            if codepoint == 0xD800:
                codepoint = 0xE000
            marker = chr(codepoint)  # exhaustion fails closed in the outer handler
        # Redact before truncation: cutting a credential in half leaks its prefix.
        raw = _redacted(text, redactions, marker).encode("utf-8", "replace")
        if len(raw) > _MAX_EXCEPTION_BYTES:
            suffix = _redacted(_TRUNCATED, redactions, marker).encode("utf-8")
            raw = raw[:_MAX_EXCEPTION_BYTES - len(suffix)].decode("utf-8", "ignore").encode("utf-8") + suffix
        # Encoding replacement and joining the truncation marker can introduce
        # new matches. Mask the final text too, then keep a valid UTF-8 prefix.
        raw = _mask(raw.decode("utf-8"), redactions, marker).encode("utf-8")
        raw = raw[:_MAX_EXCEPTION_BYTES].decode("utf-8", "ignore").encode("utf-8")
        sys.stderr.write(raw.decode("utf-8"))
        sys.stderr.flush()
    except BaseException:
        # Formatting, redaction and a closed/broken stderr must not change the exit.
        pass
