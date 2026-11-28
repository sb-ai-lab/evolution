"""Transport-neutral command layer core (ADR-0070): request descriptions, registry, path and response helpers."""
from __future__ import annotations

from dataclasses import dataclass
import enum
from typing import Any, Callable, Literal
from urllib.parse import quote


class Effect(enum.Enum):
    READ = "read"
    MUTATION = "mutation"
    IRREVERSIBLE = "irreversible"


@dataclass(frozen=True)
class Request:
    """One service request. An effect on the request overrides the command default."""

    method: str
    path: str
    schema: str | None
    payload: dict | None = None
    kind: Literal["json", "text", "bytes"] = "json"
    content_type: str | None = None
    mutation_key: tuple[str, str] | None = None
    effect: Effect | None = None


@dataclass(frozen=True)
class Command:
    """A named command: a service ``build`` or an offline ``run``, exactly one of them."""

    name: str
    effect: Effect
    needs_service: bool
    schema: str | None
    build: Callable[..., Request] | None
    run: Callable[..., Any] | None
    asks: bool = False

    def __post_init__(self) -> None:
        if (self.build is None) == (self.run is None):
            raise ValueError(f"command {self.name!r} needs exactly one of build or run")
        if self.asks and self.effect is Effect.READ:
            raise ValueError(f"read command {self.name!r} cannot ask")


REGISTRY: dict[str, Command] = {}


def command(name: str, *, effect: Effect, schema: str | None, service: bool = True, asks: bool = False):
    """Register a request builder (``service=True``) or an offline runner under ``name``."""

    def register(function: Callable[..., Any]) -> Callable[..., Any]:
        if name in REGISTRY:
            raise ValueError(f"duplicate command name {name!r}")
        REGISTRY[name] = Command(name, effect, service, schema,
                                 function if service else None, None if service else function, asks=asks)
        return function

    return register


def mutation_key(request_id: str | None, owner_generation: str | None) -> tuple[str, str] | None:
    """Describe the caller's key, including the CLI's legacy unkeyed mode."""
    if request_id is None and owner_generation is None:
        return None
    if request_id is None or owner_generation is None:
        raise ValueError("request_id and owner_generation must be given together")
    return request_id, owner_generation


def segment_path(*segments: str) -> str:
    """Join ``segments`` into a URL path, percent-encoding each one (``/`` included)."""
    return "/" + "/".join(quote(segment, safe="") for segment in segments)


def with_query(path: str, /, **values: str | None) -> str:
    """Append ``values`` as a query, dropping ``None``; the bare path when every value is ``None``."""
    pairs = [(key, value) for key, value in values.items() if value is not None]
    if not pairs:
        return path
    return path + "?" + "&".join(f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in pairs)


def check_response(request: Request, value: dict) -> dict:
    """Validate ``value`` against the request's schema (none: unvalidated); return it unchanged."""
    from evolution_lab.cli import responses  # ``cli/__init__`` is lazy: this loads no parser

    responses.validate(value, request.schema)
    return value
