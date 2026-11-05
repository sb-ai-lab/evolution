"""Memento CLI wrapper that logs per-call LLM telemetry.

Why this exists
---------------
The previous ``solver_native.run_memento_test`` invoked the ``memento``
binary directly, which produced a working subprocess but provided no
visibility into how many tokens each test row consumed. Memento drives
its LLM calls through ``litellm``, so we install a ``litellm.success_callback``
*before* importing or running Memento's CLI. The callback writes one
JSONL line per LLM call; the parent reads the file after the subprocess
exits and aggregates totals into the cached ``result.json``.

Belt-and-braces: also patch ``openai.OpenAI`` for any Memento path that
constructs an OpenAI client directly (some embedding / reranker code
paths do this).

Inputs
------
``--telemetry-jsonl <path>`` — output file for per-call usage records.
All remaining args are forwarded to ``memento`` (e.g. ``agent --message
... --no-markdown``). Memento's ``cli.main`` is invoked in-process via
``Typer.app()`` so the callbacks installed here are active during the
agent's whole lifecycle.

Memento repo path is read from ``MEMENTO_REPO`` env var. If unset, the
wrapper uses the paper workspace checkout at
``$MEMENTO_REPO``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "experiments"))

from _shared.local_paths import memento_repo  # noqa: E402

MEMENTO_REPO = memento_repo()


def _install_telemetry(jsonl_path: Path) -> None:
    """Wire litellm + openai callbacks that append to a per-call JSONL."""
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    if not jsonl_path.exists():
        jsonl_path.touch()
    handle = jsonl_path.open("a", encoding="utf-8", buffering=1)

    # ── litellm path ────────────────────────────────────────────────
    try:
        import litellm  # type: ignore

        def _litellm_cb(kwargs, completion_response, start_time, end_time):
            try:
                usage = getattr(completion_response, "usage", None)
                if isinstance(start_time, (int, float)) and isinstance(end_time, (int, float)):
                    latency_ms = round((float(end_time) - float(start_time)) * 1000, 1)
                else:
                    latency_ms = (
                        round((time.time() - float(start_time)) * 1000, 1) if start_time else 0.0
                    )
                handle.write(
                    json.dumps(
                        {
                            "source": "litellm",
                            "model": kwargs.get("model", "") if isinstance(kwargs, dict) else "",
                            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0)
                            if usage is not None
                            else 0,
                            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0)
                            if usage is not None
                            else 0,
                            "latency_ms": latency_ms,
                            "ok": True,
                        }
                    )
                    + "\n"
                )
            except Exception:
                # Telemetry is best-effort; never let logging crash the run.
                pass

        litellm.success_callback = list(getattr(litellm, "success_callback", []) or []) + [
            _litellm_cb
        ]

        # Memento uses ``litellm.acompletion`` (often with streaming).
        # litellm's success_callback / _async_success_callback don't fire
        # reliably on streaming chunks — we wrap acompletion directly so
        # every call is observed regardless of stream mode. For streaming
        # responses we async-iterate the wrapped object and capture usage
        # from the final chunk; for non-streaming we read response.usage.
        real_acompletion = litellm.acompletion

        async def _wrapped_acompletion(*args, **kwargs):
            t0 = time.time()
            model_id = kwargs.get("model", "")
            try:
                resp = await real_acompletion(*args, **kwargs)
            except Exception:
                handle.write(
                    json.dumps(
                        {
                            "source": "litellm.acompletion",
                            "model": model_id,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "latency_ms": round((time.time() - t0) * 1000, 1),
                            "ok": False,
                        }
                    )
                    + "\n"
                )
                raise
            if kwargs.get("stream"):

                async def _gen():
                    last_usage = None
                    async for chunk in resp:
                        u = getattr(chunk, "usage", None)
                        if u is not None:
                            last_usage = u
                        yield chunk
                    handle.write(
                        json.dumps(
                            {
                                "source": "litellm.acompletion.stream",
                                "model": model_id,
                                "prompt_tokens": int(getattr(last_usage, "prompt_tokens", 0) or 0)
                                if last_usage is not None
                                else 0,
                                "completion_tokens": int(
                                    getattr(last_usage, "completion_tokens", 0) or 0
                                )
                                if last_usage is not None
                                else 0,
                                "latency_ms": round((time.time() - t0) * 1000, 1),
                                "ok": True,
                            }
                        )
                        + "\n"
                    )

                return _gen()
            usage = getattr(resp, "usage", None)
            handle.write(
                json.dumps(
                    {
                        "source": "litellm.acompletion",
                        "model": model_id,
                        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0)
                        if usage is not None
                        else 0,
                        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0)
                        if usage is not None
                        else 0,
                        "latency_ms": round((time.time() - t0) * 1000, 1),
                        "ok": True,
                    }
                )
                + "\n"
            )
            return resp

        litellm.acompletion = _wrapped_acompletion
        # ``sitecustomize`` may import Memento modules before this wrapper runs
        # so it can patch benchmark path/budget settings. In that case
        # middleware.llm.llm_client has already executed
        # ``from litellm import acompletion`` and holds the original callable.
        # Replace the module-level alias too so every Memento call is logged.
        llm_client_mod = sys.modules.get("middleware.llm.llm_client")
        if llm_client_mod is not None:
            try:
                llm_client_mod.acompletion = _wrapped_acompletion
            except Exception:
                pass
    except ImportError:
        pass

    # ── openai path ────────────────────────────────────────────────
    try:
        import openai  # type: ignore

        orig_init = openai.OpenAI.__init__

        def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            orig_init(self, *args, **kwargs)
            completions = self.chat.completions
            if getattr(completions, "_telemetry_installed", False):
                return
            orig_create = completions.create

            def wrapped_create(*c_args, **c_kwargs):  # type: ignore[no-untyped-def]
                t0 = time.time()
                model_id = c_kwargs.get("model", "")
                try:
                    resp = orig_create(*c_args, **c_kwargs)
                except Exception:
                    handle.write(
                        json.dumps(
                            {
                                "source": "openai",
                                "model": model_id,
                                "prompt_tokens": 0,
                                "completion_tokens": 0,
                                "latency_ms": round((time.time() - t0) * 1000, 1),
                                "ok": False,
                            }
                        )
                        + "\n"
                    )
                    raise
                usage = getattr(resp, "usage", None)
                handle.write(
                    json.dumps(
                        {
                            "source": "openai",
                            "model": model_id,
                            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0)
                            if usage is not None
                            else 0,
                            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0)
                            if usage is not None
                            else 0,
                            "latency_ms": round((time.time() - t0) * 1000, 1),
                            "ok": True,
                        }
                    )
                    + "\n"
                )
                return resp

            completions.create = wrapped_create
            completions._telemetry_installed = True  # type: ignore[attr-defined]

        openai.OpenAI.__init__ = patched_init
    except ImportError:
        pass


def _pin_session_id(pinned: str) -> None:
    """Force the FIRST session created in this process to use ``pinned``.

    The pilot orchestrator pre-creates the agent sandbox at the path
    Memento will derive from the session id (``workspace_dir/<date>/
    md5(sid)[:8]``) and pre-copies task inputs into it. For that to work,
    we must control the session id Memento picks.

    Memento's CLI bypasses ``shared.chat.generate_session_id``: in
    cli/commands/agent.py, when no ``--session`` is passed, the agent
    calls ``ChatManager.create_session`` which dispatches to
    ``SessionService._create`` and the SQLAlchemy ``Session`` model's
    ``id`` column has ``default=lambda: str(uuid.uuid4())``. The id is
    only known *after* commit. Patch ``SessionService._create`` to set
    ``obj.id = pinned`` on first invocation; subsequent creations fall
    through to the real default (uuid).
    """
    sys.path.insert(0, str(MEMENTO_REPO))
    try:
        from middleware.storage.services import session_service as _ss
    except Exception:
        return
    real_create = _ss.SessionService._create
    fired = {"once": False}

    async def _patched_create(self, db, data):  # type: ignore[no-untyped-def]
        if fired["once"]:
            return await real_create(self, db, data)
        fired["once"] = True
        from middleware.storage.models import Session as _Sess
        from middleware.storage.models import SessionStatus
        from middleware.storage.schemas import SessionRead
        from middleware.storage.utils import get_east_8_time

        obj = _Sess(
            id=pinned,
            title=data.title,
            description=data.description,
            meta_info=data.meta_info,
            status=SessionStatus.ACTIVE.value,
            created_at=get_east_8_time(),
            updated_at=get_east_8_time(),
        )
        db.add(obj)
        await db.commit()
        await db.refresh(obj)
        return SessionRead.model_validate(obj)

    _ss.SessionService._create = _patched_create


def main() -> int:
    # Parse only --telemetry-jsonl from the front; everything else is
    # forwarded to Memento's CLI (which uses Typer and reads sys.argv).
    argv = sys.argv[1:]
    telemetry_path: Path | None = None
    forwarded: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--telemetry-jsonl" and i + 1 < len(argv):
            telemetry_path = Path(argv[i + 1])
            i += 2
            continue
        forwarded.append(a)
        i += 1

    if telemetry_path is None:
        print("--telemetry-jsonl is required", file=sys.stderr)
        return 2

    _install_telemetry(telemetry_path)

    if str(MEMENTO_REPO) not in sys.path:
        sys.path.insert(0, str(MEMENTO_REPO))

    # Pin session id BEFORE importing cli.main (so cli/commands/agent.py's
    # `from shared.chat import generate_session_id` picks up the patched
    # function reference, not the real one).
    pinned = os.environ.get("MEMENTO_PILOT_SESSION_ID")
    if pinned:
        _pin_session_id(pinned)

    # Re-write argv so Typer sees only the forwarded args, with the
    # script name set to "memento" for help-text consistency.
    sys.argv = ["memento", *forwarded]

    from cli.main import app  # type: ignore

    try:
        app()
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)


if __name__ == "__main__":
    sys.exit(main())
