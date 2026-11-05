"""Unified LLM token / latency telemetry for the experiment family.

Why this lives here, not in ``evolution/src/evolution/llm/``:
``LLMResponse`` (in ``evolution/src/evolution/llm/backend.py``) already
carries ``tokens_in / tokens_out / time_ms``; ``LiteLLMBackend``
populates them. But Hermes / Memento / MemP bypass that
backend — they call OpenAI / litellm directly, often inside subprocesses
— so the cross-framework telemetry layer is the experiment driver, not
the framework backend.

Two pieces:

* ``TelemetryRecord`` — the canonical JSONL line shape.
* ``wrap_openai_client(client, recorder, *, source)`` — monkey-patches
  ``client.chat.completions.create`` (and stream-wrappers) to emit one
  ``TelemetryRecord`` per call.
* ``JsonlRecorder(path)`` — context manager. Line-buffered append-only
  writer; safe for one writer per file (subprocesses each get their own
  per-trial path so contention is avoided by construction).

Schema (one JSON object per line):

    {
      "ts":         time.time() at call start (float),
      "source":    "evolution"|"hermes"|"memento"|"memp"|"judge"|"smoke",
      "model":     str (provider-side model id),
      "prompt_tokens":     int,
      "completion_tokens": int,
      "latency_ms":        float,
      "ok":                bool,
      "stream":            bool   (only present when streaming),
      "error":             str    (only present on failure)
    }

The legacy reader at ``4way-sbbench-pilot/solver_native.py:_read_telemetry_jsonl``
already accepts this shape.

Secrets hygiene (memory ``feedback_secret_hygiene.md``): we never write
API keys, headers, or message bodies — only the four counted fields plus
a model id and (on failure) a 300-char repr-truncated error.
"""

from __future__ import annotations

import json
import time
from contextlib import AbstractContextManager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any

VALID_SOURCES = (
    "evolution",
    "hermes",
    "codex",
    "claude",
    "memento",
    "memp",
    "evoskill",
    "judge",
    "smoke",
)


@dataclass
class TelemetryRecord:
    ts: float
    source: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    ok: bool
    stream: bool = False
    error: str = ""

    def to_jsonl(self) -> str:
        # Drop ``stream`` and ``error`` when default to keep lines compact and
        # match the existing schema produced by the Hermes wrapper.
        d: dict[str, Any] = {
            "ts": round(self.ts, 3),
            "source": self.source,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": round(self.latency_ms, 1),
            "ok": self.ok,
        }
        if self.stream:
            d["stream"] = True
        if self.error:
            d["error"] = self.error
        return json.dumps(d, ensure_ascii=False)


@dataclass
class _RecorderState:
    path: Path
    handle: IO[str] | None = None
    closed: bool = False
    records: int = 0


class JsonlRecorder(AbstractContextManager):
    """Append-only JSONL recorder. One writer per file."""

    def __init__(self, path: Path) -> None:
        self._state = _RecorderState(path=Path(path))

    def __enter__(self) -> JsonlRecorder:
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        self._state.path.parent.mkdir(parents=True, exist_ok=True)
        if not self._state.path.exists():
            self._state.path.touch()
        # Line-buffered append; subprocesses get their own per-trial path.
        self._state.handle = self._state.path.open("a", encoding="utf-8", buffering=1)
        self._state.closed = False

    def close(self) -> None:
        if self._state.handle and not self._state.closed:
            try:
                self._state.handle.flush()
            finally:
                self._state.handle.close()
                self._state.closed = True

    def record(self, rec: TelemetryRecord) -> None:
        if rec.source not in VALID_SOURCES:
            # Don't silently drop — but also don't crash a long-running run.
            rec = TelemetryRecord(**{**asdict(rec), "source": "smoke"})
        if self._state.handle is None:
            self.open()
        assert self._state.handle is not None
        self._state.handle.write(rec.to_jsonl() + "\n")
        self._state.records += 1


def _usage_from_response(resp: Any) -> Any:
    """Return the ``usage`` object from an OpenAI response.

    litellm drives the OpenAI SDK via its raw-response path, so ``create``
    returns a ``LegacyAPIResponse`` with no ``.usage`` until ``.parse()`` is
    called (parse is cached, so this does not disturb litellm's own parse).
    Direct-client callers already get a parsed response with ``.usage``.
    """
    usage = getattr(resp, "usage", None)
    if usage is None and hasattr(resp, "parse"):
        try:
            usage = getattr(resp.parse(), "usage", None)
        except Exception:
            usage = None
    return usage


def wrap_openai_client(
    client: Any,
    recorder: JsonlRecorder,
    *,
    source: str,
    extra_body: dict | None = None,
) -> Any:
    """Monkey-patch ``client.chat.completions.create`` to emit telemetry.

    Returns the same client (mutated). Idempotent: a second call on the
    same client is a no-op.

    The wrapper handles both non-streaming and streaming responses. For
    streams, usage is captured from the final chunk (when the provider
    reports it) and one TelemetryRecord is emitted per stream. On
    exception, a ``ok=False`` record is emitted before re-raising.

    ``extra_body`` is merged into every call; caller's own ``extra_body``
    wins on key conflict. Typical use: inject Qwen3
    ``chat_template_kwargs.enable_thinking=False`` at the wrapper layer
    so callers don't have to thread it through.

    Compatibility: this wraps an instance's ``chat.completions.create``
    method. To wrap *all* OpenAI clients constructed within a process
    (e.g. when the framework constructs its own client), wrap the class
    via ``patch_openai_class()`` below instead.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES!r}, got {source!r}")
    completions = client.chat.completions
    if getattr(completions, "_telemetry_installed", False):
        return client
    orig_create = completions.create

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        if extra_body:
            merged = dict(extra_body)
            if isinstance(kwargs.get("extra_body"), dict):
                merged = {**merged, **kwargs["extra_body"]}
            kwargs["extra_body"] = merged
        ts = time.time()
        t0 = time.monotonic()
        model_id = str(kwargs.get("model", ""))
        stream_mode = bool(kwargs.get("stream"))
        try:
            resp = orig_create(*args, **kwargs)
        except Exception as exc:
            recorder.record(
                TelemetryRecord(
                    ts=ts,
                    source=source,
                    model=model_id,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=(time.monotonic() - t0) * 1000.0,
                    ok=False,
                    error=repr(exc)[:300],
                )
            )
            raise
        if stream_mode:

            def _gen() -> Any:
                last_usage = None
                for chunk in resp:
                    u = getattr(chunk, "usage", None)
                    if u is not None:
                        last_usage = u
                    yield chunk
                recorder.record(
                    TelemetryRecord(
                        ts=ts,
                        source=source,
                        model=model_id,
                        prompt_tokens=int(getattr(last_usage, "prompt_tokens", 0) or 0)
                        if last_usage is not None
                        else 0,
                        completion_tokens=int(getattr(last_usage, "completion_tokens", 0) or 0)
                        if last_usage is not None
                        else 0,
                        latency_ms=(time.monotonic() - t0) * 1000.0,
                        ok=True,
                        stream=True,
                    )
                )

            class _Wrapped:
                def __init__(self, real: Any, gen: Any) -> None:
                    self._real = real
                    self._gen = gen

                def __iter__(self) -> Any:
                    return self._gen

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._real, name)

            return _Wrapped(resp, _gen())
        usage = _usage_from_response(resp)
        recorder.record(
            TelemetryRecord(
                ts=ts,
                source=source,
                model=model_id,
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
                latency_ms=(time.monotonic() - t0) * 1000.0,
                ok=True,
            )
        )
        return resp

    completions.create = _wrapped
    completions._telemetry_installed = True  # type: ignore[attr-defined]
    return client


def wrap_async_openai_client(
    client: Any,
    recorder: JsonlRecorder,
    *,
    source: str,
    extra_body: dict | None = None,
) -> Any:
    """Async counterpart of ``wrap_openai_client`` for ``AsyncOpenAI``.

    Needed for frameworks (e.g. EvoSkill) whose client calls
    ``await client.chat.completions.create(...)``; the sync wrapper would
    record against an un-awaited coroutine (0 tokens) and break streams.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES!r}, got {source!r}")
    completions = client.chat.completions
    if getattr(completions, "_telemetry_installed", False):
        return client
    orig_create = completions.create

    async def _wrapped(*args: Any, **kwargs: Any) -> Any:
        if extra_body:
            merged = dict(extra_body)
            if isinstance(kwargs.get("extra_body"), dict):
                merged = {**merged, **kwargs["extra_body"]}
            kwargs["extra_body"] = merged
        ts = time.time()
        t0 = time.monotonic()
        model_id = str(kwargs.get("model", ""))
        stream_mode = bool(kwargs.get("stream"))
        try:
            resp = await orig_create(*args, **kwargs)
        except Exception as exc:
            recorder.record(
                TelemetryRecord(
                    ts=ts,
                    source=source,
                    model=model_id,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=(time.monotonic() - t0) * 1000.0,
                    ok=False,
                    error=repr(exc)[:300],
                )
            )
            raise
        if stream_mode:

            async def _agen() -> Any:
                last_usage = None
                async for chunk in resp:
                    u = getattr(chunk, "usage", None)
                    if u is not None:
                        last_usage = u
                    yield chunk
                recorder.record(
                    TelemetryRecord(
                        ts=ts,
                        source=source,
                        model=model_id,
                        prompt_tokens=int(getattr(last_usage, "prompt_tokens", 0) or 0)
                        if last_usage is not None
                        else 0,
                        completion_tokens=int(getattr(last_usage, "completion_tokens", 0) or 0)
                        if last_usage is not None
                        else 0,
                        latency_ms=(time.monotonic() - t0) * 1000.0,
                        ok=True,
                        stream=True,
                    )
                )

            return _agen()
        usage = _usage_from_response(resp)
        recorder.record(
            TelemetryRecord(
                ts=ts,
                source=source,
                model=model_id,
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
                latency_ms=(time.monotonic() - t0) * 1000.0,
                ok=True,
            )
        )
        return resp

    completions.create = _wrapped
    completions._telemetry_installed = True  # type: ignore[attr-defined]
    return client


def patch_openai_class(
    recorder: JsonlRecorder,
    *,
    source: str,
    extra_body: dict | None = None,
) -> None:
    """Patch ``openai.OpenAI`` and ``openai.AsyncOpenAI`` so every client
    built afterwards is wrapped.

    Equivalent to ``wrap_openai_client`` but applied at class
    construction time — needed when the framework (e.g. Hermes, EvoSkill)
    builds its own client inside a subprocess and we don't have a handle.

    Idempotent on a per-class basis.
    """
    import openai  # type: ignore

    if not getattr(openai.OpenAI, "_telemetry_installed", False):
        orig_init = openai.OpenAI.__init__

        def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
            orig_init(self, *args, **kwargs)
            wrap_openai_client(self, recorder, source=source, extra_body=extra_body)

        openai.OpenAI.__init__ = _patched_init  # type: ignore[method-assign]
        openai.OpenAI._telemetry_installed = True  # type: ignore[attr-defined]
        openai.OpenAI._telemetry_orig_init = orig_init  # type: ignore[attr-defined]

    async_cls = getattr(openai, "AsyncOpenAI", None)
    if async_cls is not None and not getattr(async_cls, "_telemetry_installed", False):
        orig_ainit = async_cls.__init__

        def _patched_ainit(self: Any, *args: Any, **kwargs: Any) -> None:
            orig_ainit(self, *args, **kwargs)
            wrap_async_openai_client(self, recorder, source=source, extra_body=extra_body)

        async_cls.__init__ = _patched_ainit  # type: ignore[method-assign]
        async_cls._telemetry_installed = True  # type: ignore[attr-defined]
        async_cls._telemetry_orig_init = orig_ainit  # type: ignore[attr-defined]


def uninstall_openai_class_patch() -> None:
    """Undo ``patch_openai_class``: restore the original client ``__init__`` so
    clients built afterwards are unwrapped. Produce telemetry is scoped to the
    produce stage; without this the class patch + a closed recorder make every
    later in-process call (e.g. eval) raise on ``recorder.record``."""
    try:
        import openai  # type: ignore
    except ImportError:
        return
    for cls_name in ("OpenAI", "AsyncOpenAI"):
        cls = getattr(openai, cls_name, None)
        if cls is None:
            continue
        orig = getattr(cls, "_telemetry_orig_init", None)
        if orig is not None:
            cls.__init__ = orig  # type: ignore[method-assign]
            with suppress(AttributeError):
                delattr(cls, "_telemetry_orig_init")
        with suppress(AttributeError):
            delattr(cls, "_telemetry_installed")


def install_litellm_telemetry(recorder: JsonlRecorder, *, source: str) -> bool:
    """Wrap ``litellm.completion`` and ``litellm.acompletion`` so every call
    emits one ``TelemetryRecord``.

    Direct wrapping (not ``litellm.success_callback``) is used on purpose: a
    callback + acompletion-wrap mix double-counts non-streaming async calls,
    and the async/streaming callback paths fire unreliably. One record per
    call. Idempotent per-process. Returns False if litellm is not importable.
    """
    try:
        import litellm  # type: ignore
    except ImportError:
        return False
    if getattr(litellm, "_evo_produce_telemetry_installed", False):
        return True

    def _rec(model_id: str, usage: Any, t0: float, *, ok: bool, stream: bool = False) -> None:
        recorder.record(
            TelemetryRecord(
                ts=time.time(),
                source=source,
                model=str(model_id or ""),
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
                latency_ms=(time.monotonic() - t0) * 1000.0,
                ok=ok,
                stream=stream,
            )
        )

    real_completion = litellm.completion

    def _wrapped_completion(*args: Any, **kwargs: Any) -> Any:
        t0 = time.monotonic()
        model_id = kwargs.get("model", "")
        try:
            resp = real_completion(*args, **kwargs)
        except Exception:
            _rec(model_id, None, t0, ok=False)
            raise
        _rec(model_id, getattr(resp, "usage", None), t0, ok=True)
        return resp

    real_acompletion = litellm.acompletion

    async def _wrapped_acompletion(*args: Any, **kwargs: Any) -> Any:
        t0 = time.monotonic()
        model_id = kwargs.get("model", "")
        try:
            resp = await real_acompletion(*args, **kwargs)
        except Exception:
            _rec(model_id, None, t0, ok=False)
            raise
        if kwargs.get("stream"):

            async def _gen() -> Any:
                last_usage = None
                async for chunk in resp:
                    u = getattr(chunk, "usage", None)
                    if u is not None:
                        last_usage = u
                    yield chunk
                _rec(model_id, last_usage, t0, ok=True, stream=True)

            return _gen()
        _rec(model_id, getattr(resp, "usage", None), t0, ok=True)
        return resp

    litellm.completion = _wrapped_completion
    litellm.acompletion = _wrapped_acompletion
    litellm._evo_produce_telemetry_installed = True  # type: ignore[attr-defined]
    return True


def install_produce_telemetry(jsonl_path: Path, *, source: str) -> JsonlRecorder:
    """Open a recorder at ``jsonl_path`` and patch the openai client classes so
    in-process produce calls are counted exactly once. Returns the open recorder
    (caller closes it).

    Only the openai SDK is patched (not litellm): against this experiment's
    openai-compatible local endpoint litellm routes every call through
    ``openai.AsyncOpenAI``, so the class patch already captures litellm-backed
    frameworks (evolution) and raw-openai frameworks (memp/hermes/evoskill)
    once each. Wrapping litellm too would double-count the litellm callers."""
    recorder = JsonlRecorder(Path(jsonl_path))
    recorder.open()
    patch_openai_class(recorder, source=source)
    return recorder


def read_telemetry_jsonl(path: Path) -> tuple[list[dict], dict]:
    """Read JSONL records and return ``(per_turn, totals)``.

    Same shape as the legacy reader so existing aggregation code in
    ``solver_native.py`` keeps working unchanged.
    """
    per_turn: list[dict] = []
    parse_errors = 0
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                parse_errors += 1
                continue
            per_turn.append(
                {
                    "ts": float(rec.get("ts", 0.0) or 0.0),
                    "tokens_in": int(rec.get("prompt_tokens", 0) or 0),
                    "tokens_out": int(rec.get("completion_tokens", 0) or 0),
                    "latency_ms": float(rec.get("latency_ms", 0.0) or 0.0),
                    "model_id": rec.get("model", ""),
                    "source": rec.get("source", ""),
                    "ok": bool(rec.get("ok", True)),
                }
            )
    totals = {
        "tokens_in_total": sum(t["tokens_in"] for t in per_turn),
        "tokens_out_total": sum(t["tokens_out"] for t in per_turn),
        "latency_ms_total": round(sum(t["latency_ms"] for t in per_turn), 1),
        "llm_calls": len(per_turn),
        "telemetry_parse_errors": parse_errors,
    }
    return per_turn, totals


def usage_from_result(res: dict) -> dict:
    """Normalize a runner/solve result's token totals to the usage shape."""
    return {
        "llm_calls": int(res.get("llm_calls") or 0),
        "tokens_in": int(res.get("tokens_in_total") or res.get("tokens_in") or 0),
        "tokens_out": int(res.get("tokens_out_total") or res.get("tokens_out") or 0),
        "latency_ms": float(res.get("latency_ms_total") or res.get("latency_ms") or 0.0),
    }


def add_usage(*usages: dict | None) -> dict:
    total = {"llm_calls": 0, "tokens_in": 0, "tokens_out": 0, "latency_ms": 0.0}
    for usage in usages:
        if not usage:
            continue
        total["llm_calls"] += int(usage.get("llm_calls") or 0)
        total["tokens_in"] += int(usage.get("tokens_in") or 0)
        total["tokens_out"] += int(usage.get("tokens_out") or 0)
        total["latency_ms"] += float(usage.get("latency_ms") or 0.0)
    total["latency_ms"] = round(total["latency_ms"], 1)
    return total


__all__ = [
    "JsonlRecorder",
    "TelemetryRecord",
    "VALID_SOURCES",
    "add_usage",
    "install_litellm_telemetry",
    "install_produce_telemetry",
    "patch_openai_class",
    "read_telemetry_jsonl",
    "usage_from_result",
    "wrap_async_openai_client",
    "wrap_openai_client",
]
