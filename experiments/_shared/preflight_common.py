"""Shared preflight helpers used by sb-bench pilots.

Promoted from §5.1's inline helpers (Round 0e).

Functions:

* ``load_env_silent(path)``: dotenv-load without ever logging values.
* ``smoke_chat(model_id, base, key, *, extra_body=None, prompt='Reply OK')``:
  one chat-completion call, returns latency + tokens. Used to healthcheck
  cloud + local providers.
* ``discover_local_served_id(api_base)``: hit ``/v1/models`` to learn the
  served-id of the running sglang. The §5.1 pilot needed this so the
  manifest's ``served_id_hint`` could be cross-checked at run time.
* ``healthcheck_endpoint(model_cfg)``: combine the two — load key, run
  smoke chat, return a ``CheckResult``.
* ``home_isolation_gate(*paths)``: refuse to run if any of the supplied
  framework HOMEs already exists without an archive suffix; if it does,
  archive with ``_YYYYMMDD-HHMMSS``.

Secrets never leave this module: API keys are read from env vars only,
never logged or returned in result objects.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Env loading (silent)
# ---------------------------------------------------------------------------


def load_env_silent(env_path: Path) -> bool:
    """Load .env without ever logging or returning values.

    Returns True if at least one variable was set in env.
    """
    if not env_path.is_file():
        return False
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(env_path, override=False)
        return True
    except ImportError:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return True


# ---------------------------------------------------------------------------
# Endpoint discovery + smoke chat
# ---------------------------------------------------------------------------


def discover_local_served_id(api_base: str, *, timeout_s: float = 5.0) -> str | None:
    """Hit ``<api_base>/models`` and return the first served-id, or None.

    Used by the §5.2 preflight to confirm the manifest's
    ``served_id_hint`` matches what sglang currently exposes.
    """
    url = api_base.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        data = payload.get("data") or []
        if not data:
            return None
        return str(data[0].get("id") or "") or None
    except Exception:
        return None


@dataclass
class SmokeResult:
    label: str
    model_id: str
    ok: bool
    tokens_in: int
    tokens_out: int
    latency_ms: float
    reply_tail: str
    error: str = ""


def smoke_chat(
    model_id: str,
    *,
    base: str,
    key: str,
    label: str = "smoke",
    extra_body: dict | None = None,
    prompt: str = "Reply with exactly: OK",
    max_tokens: int = 32,
    timeout_s: float = 60.0,
) -> SmokeResult:
    """One chat-completion against the given endpoint.

    Returns a ``SmokeResult`` regardless of success — caller checks
    ``ok``. The reply tail is truncated to 32 chars; the API key is never
    included in the result (only env-var-loaded into the OpenAI client).
    """
    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=base.rstrip("/"), timeout=timeout_s)
    started = time.time()
    try:
        kwargs: dict[str, Any] = dict(
            model=model_id,
            max_tokens=max_tokens,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        if extra_body:
            kwargs["extra_body"] = extra_body
        r = client.chat.completions.create(**kwargs)
    except Exception as exc:
        return SmokeResult(
            label=label,
            model_id=model_id,
            ok=False,
            tokens_in=0,
            tokens_out=0,
            latency_ms=round((time.time() - started) * 1000, 1),
            reply_tail="",
            error=repr(exc)[:300],
        )
    dt_ms = (time.time() - started) * 1000
    content = (r.choices[0].message.content or "").strip()
    usage = getattr(r, "usage", None)
    return SmokeResult(
        label=label,
        model_id=model_id,
        ok="OK" in content[:32],
        tokens_in=int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
        tokens_out=int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
        latency_ms=round(dt_ms, 1),
        reply_tail=content[:32],
    )


# ---------------------------------------------------------------------------
# Endpoint healthcheck (combined)
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """One preflight check result. ``detail`` MUST never include API keys."""

    name: str
    passed: bool
    detail: str = ""
    metrics: dict = field(default_factory=dict)


def healthcheck_endpoint(model_cfg: dict, *, label: str) -> CheckResult:
    """Smoke a single (api_base, model_id) tuple as configured.

    ``model_cfg`` is the dict from the §5.2 manifest:

      {
        "id": "<model id>",
        "served_id_hint": "<optional id sglang expects>",
        "api_base": "<base URL>",
        "api_key_env": "<env var holding the key>",
        "extra_body": {<optional>}
      }
    """
    api_base = model_cfg["api_base"].rstrip("/")
    key = os.environ.get(model_cfg["api_key_env"], "") or "EMPTY"
    extra_body = model_cfg.get("extra_body") or None
    served_id = model_cfg.get("served_id_hint") or model_cfg["id"]
    # Local sglang refuses an empty bearer; OpenAI SDK refuses to even
    # initialise without OPENAI_API_KEY in env.
    os.environ.setdefault("OPENAI_API_KEY", key)
    s = smoke_chat(served_id, base=api_base, key=key, label=label, extra_body=extra_body)
    if not s.ok:
        return CheckResult(
            name=f"{label}/chat",
            passed=False,
            detail=f"reply_tail={s.reply_tail!r} error={s.error!r}",
            metrics={"latency_ms": s.latency_ms},
        )
    return CheckResult(
        name=f"{label}/chat",
        passed=True,
        detail=(
            f"model={s.model_id} base={api_base} "
            f"tokens_in={s.tokens_in} tokens_out={s.tokens_out} "
            f"latency_ms={s.latency_ms}"
        ),
        metrics={
            "tokens_in": s.tokens_in,
            "tokens_out": s.tokens_out,
            "latency_ms": s.latency_ms,
        },
    )


# ---------------------------------------------------------------------------
# HOME isolation gate
# ---------------------------------------------------------------------------


def home_isolation_gate(
    *home_paths: Path,
    archive_suffix_template: str = "_%Y%m%d-%H%M%S",
) -> dict:
    """Refuse to clobber pre-existing framework HOMEs.

    For each pre-existing path, archive it with a timestamp suffix; if a
    suffix-collision occurs (extremely unlikely; same second twice), append
    a numeric salt rather than overwriting.

    Returns ``{path_str: archive_target_str}`` for every archived path.
    """
    archived: dict = {}
    suffix_base = datetime.now().strftime(archive_suffix_template)
    for raw in home_paths:
        p = Path(os.path.expanduser(str(raw))).resolve()
        if not p.exists():
            continue
        target = p.with_name(p.name + suffix_base)
        salt = 0
        while target.exists():
            salt += 1
            target = p.with_name(p.name + suffix_base + f"-{salt}")
        shutil.move(str(p), str(target))
        archived[str(p)] = str(target)
    return archived


__all__ = [
    "CheckResult",
    "SmokeResult",
    "discover_local_served_id",
    "healthcheck_endpoint",
    "home_isolation_gate",
    "load_env_silent",
    "smoke_chat",
]
