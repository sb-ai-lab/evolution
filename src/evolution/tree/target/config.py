"""Target transport and probe values with their shared URL and path rules."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote_to_bytes, urlsplit

from evolution.tree.execution.config import ManifestError
from evolution.tree.execution.interaction import SeedMode

DEFAULT_MAX_RESPONSE_BYTES = 67108864


DEFAULT_MIN_INPUT_BYTES = 1048576


DEFAULT_CONNECT_SECONDS = 5.0


DEFAULT_READ_SECONDS = 120.0


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


_MAX_PROBE_PATH_BYTES = 2048


@dataclass(frozen=True)
class Timeouts:
    connect_seconds: float = DEFAULT_CONNECT_SECONDS
    read_seconds: float = DEFAULT_READ_SECONDS


def _normalized_loopback(base_url: str) -> str:
    """The loopback rule, and the normal form every consumer may then assume.

    It lives on `TargetBinding` rather than in the loader because it is an
    invariant of the value, not of the document: a binding built in Python — by
    a test, by an embedder — must be as unable to name a remote host as one read
    from a manifest, and `HttpTransport` must be able to split `base_url` without
    re-deciding anything.
    """
    try:
        parts = urlsplit(base_url)
        port = parts.port
    except ValueError:
        raise ValueError("target base_url is not a loopback http URL") from None
    host = parts.hostname
    if (
        parts.scheme != "http"
        or host not in _LOOPBACK_HOSTS
        or port is None
        or not (1 <= port <= 65535)
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise ValueError("target base_url is not a loopback http URL")
    assert host is not None
    host_for_url = f"[{host}]" if ":" in host else host
    return f"http://{host_for_url}:{port}"


@dataclass(frozen=True)
class TargetBinding:
    base_url: str
    seed_mode: SeedMode
    contract_digest: str | None
    settings_hash: str | None
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    min_input_bytes: int = DEFAULT_MIN_INPUT_BYTES
    timeouts: Timeouts = Timeouts()

    @property
    def preflight_sample_retries(self) -> int | None:
        """Older surfaces retain their original dataclass field payload."""
        return None

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _normalized_loopback(self.base_url))


@dataclass(frozen=True)
class TargetBindingV16(TargetBinding):
    """Only /16 stores the declared preflight retry override."""

    preflight_sample_retries: int | None = None


@dataclass(frozen=True)
class ProbeSection:
    """Bounded mediated HTTP access to an operator-owned disposable environment API."""

    base_url: str
    health_path: str = "/health"
    methods: tuple[str, ...] = ("GET", "POST")
    budget: int = 32
    request_timeout_seconds: float = 60.0
    description: str | None = None
    string_body: Literal["send", "parse"] | None = None
    on_unavailable: Literal["suspend", "continue"] | None = None


def _validate_probe_path(value: object, path: str) -> str:
    """Return one unambiguous absolute probe request target.

    The health check and later mediated requests share this validator so they
    cannot disagree about which path is safe. Query strings are retained, but
    fragments, authorities, traversal, encoded separators and multiply encoded
    destinations are refused rather than normalized.
    """
    if not isinstance(value, str):
        raise ManifestError("bad_type", path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ManifestError("bad_value", path) from None
    if (
        len(encoded) > _MAX_PROBE_PATH_BYTES
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or "#" in value
        or any(unicodedata.category(ch).startswith("C") for ch in value)
    ):
        raise ManifestError("bad_value", path)

    index = 0
    hexdigits = frozenset("0123456789abcdefABCDEF")
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or any(
            ch not in hexdigits for ch in value[index + 1 : index + 3]
        ):
            raise ManifestError("bad_value", path)
        octet = int(value[index + 1 : index + 3], 16)
        # Encoded separators can change routing after a downstream decode.
        # Encoded percent permits a second decode with the same problem.
        if octet in (0x25, 0x2F, 0x5C) or octet < 0x20 or octet == 0x7F:
            raise ManifestError("bad_value", path)
        index += 3

    # A query begins only at a literal delimiter. Decode the raw path after
    # making that split so an encoded question mark cannot hide later path
    # segments. Decode the complete target separately to catch controls in the
    # query as well.
    raw_path = value.split("?", 1)[0]
    try:
        decoded_target = unquote_to_bytes(value).decode("utf-8")
        decoded_path = unquote_to_bytes(raw_path).decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError("bad_value", path) from None
    if any(unicodedata.category(ch).startswith("C") for ch in decoded_target):
        raise ManifestError("bad_value", path)
    if any(segment in (".", "..") for segment in decoded_path.split("/")):
        raise ManifestError("bad_value", path)
    return value
