"""MemP adapter — drive native ``Memory.process_trajectory_item`` per train task.

Thin wrapper that runs MemP's native step-env episode loop per train task,
captures trajectories, and feeds them through ``process_trajectory_item``
+ ``process_trajectory_item_reflect`` (which routes through MemP's own
``adjust_memory`` on failure). Persists the resulting ``Memory`` to
``MEMP_HOME/memory/``; the test runtime calls ``Memory.retrieve`` against
that persisted memory at solve time.

The adapter does NOT emit any synthetic SKILL.md — the orchestrator's
test runner (see ``solver_native.py``) loads the persisted Memory and
queries it directly per held-out task.

Required monkey-patches (kept — these swap model endpoints, not MemP behavior):
  * ``ProcedureMem.llm_api.get_embedding_model`` → configured embedding backend
    (OpenAI-compatible by default; local sentence-transformers when
    ``MEMP_EMBEDDING_BACKEND=sentence_transformers``).
  * ``ProcedureMem.llm_api.llm`` and ``.get_llm_response`` → OpenRouter
    Gemini (used by ``process_trajectory_item`` + ``adjust_memory``).
  * ``ProcedureMem.memory_adjust.llm`` → same Gemini, string-returning.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ._envelopes import PROC_MEMORY_DONE_SUMMARY
from ._memp_env import TaskEnv
from .common import PilotManifest

logger = logging.getLogger(__name__)

FRAMEWORK_TAG = "memp"
MEMP_REPO = Path(os.environ.get("MEMP_REPO", ""))
MEMP_HOME = Path.home() / ".memp_sbbench"

# Trace-fidelity caps for MemP's evolve signal; env-gated, defaults generous enough to preserve real AFTER content.
_INSTR_CHARS = int(os.environ.get("EVO_MEMP_INSTR_CHARS", "60000"))
_QUERY_CHARS = int(os.environ.get("EVO_MEMP_QUERY_CHARS", "60000"))
_WORKFLOW_CHARS = int(os.environ.get("EVO_MEMP_WORKFLOW_CHARS", "40000"))
_OBS_CHARS = int(os.environ.get("EVO_MEMP_OBS_CHARS", "60000"))
_MSG_CHARS = int(os.environ.get("EVO_MEMP_MSG_CHARS", "60000"))
_ACTION_CHARS = int(os.environ.get("EVO_MEMP_ACTION_CHARS", "48000"))
_TRAJ_OBS_CHARS = int(os.environ.get("EVO_MEMP_TRAJ_OBS_CHARS", "24000"))
_TRAJ_CHARS = int(os.environ.get("EVO_MEMP_TRAJ_CHARS", "120000"))

# Module-level reflector-call telemetry. Reset on every
# ``install_monkeypatches`` call. Mutated by the patched LLM closure.
_REFLECTOR_STATS: dict[str, int] = {
    "calls_total": 0,
    "call_errors": 0,
    "parse_errors": 0,
    "successes": 0,
}
_ORIGINAL_WORKFLOW_PROMPT = None


class _SentenceTransformerEmbeddings:
    """Minimal LangChain Embeddings-shaped wrapper for local MemP retrieval."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = model_name
        cache_folder = (
            os.environ.get("SENTENCE_TRANSFORMERS_HOME")
            or os.environ.get("HF_HOME")
            or str(Path.home() / "research" / ".cache" / "huggingface")
        )
        device = os.environ.get("MEMP_EMBEDDING_DEVICE")
        kwargs = {"cache_folder": cache_folder}
        if device:
            kwargs["device"] = device
        self._model = SentenceTransformer(model_name, **kwargs)

    def embed_query(self, text: str) -> list[float]:
        vec = self._model.encode(
            [str(text)],
            normalize_embeddings=True,
            show_progress_bar=False,
        )[0]
        return [float(x) for x in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = self._model.encode(
            [str(t) for t in texts],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(x) for x in row] for row in vecs]


@dataclass
class MempProducePlan:
    framework: str = FRAMEWORK_TAG
    per_task_skills: dict[str, list[str]] = field(default_factory=dict)
    all_produced_skill_names: list[str] = field(default_factory=list)
    all_produced_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    memory_dir: str = ""
    n_train_trajectories: int = 0
    n_unique_workflows: int = 0
    n_reflect_adjusted: int = 0
    n_reflector_calls: int = 0
    n_reflector_call_errors: int = 0
    n_reflector_parse_errors: int = 0
    n_reflector_successes: int = 0
    runtime_seconds: float = 0.0

    def to_jsonable(self) -> dict:
        return {
            "framework": self.framework,
            "per_task_skills": self.per_task_skills,
            "all_produced_skill_names": self.all_produced_skill_names,
            "all_produced_files": self.all_produced_files,
            "notes": self.notes,
            "attempts": self.attempts,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "memory_dir": self.memory_dir,
            "n_train_trajectories": self.n_train_trajectories,
            "n_unique_workflows": self.n_unique_workflows,
            "n_reflect_adjusted": self.n_reflect_adjusted,
            "n_reflector_calls": self.n_reflector_calls,
            "n_reflector_call_errors": self.n_reflector_call_errors,
            "n_reflector_parse_errors": self.n_reflector_parse_errors,
            "n_reflector_successes": self.n_reflector_successes,
            "runtime_seconds": self.runtime_seconds,
        }


def _ensure_memp_path() -> None:
    if str(MEMP_REPO) not in sys.path:
        sys.path.insert(0, str(MEMP_REPO))


def _documents_json_path(memory_dir: Path) -> Path:
    memory_dir = Path(memory_dir)
    native_path = memory_dir / "direct" / "documents.json"
    if native_path.is_file():
        return native_path
    return memory_dir / "documents.json"


def install_monkeypatches(
    *,
    embedding_model: str,
    embedding_base: str,
    embedding_key: str,
    reflector_model: str,
    reflector_base: str,
    reflector_key: str,
) -> None:
    """Install MemP's embedding + reflector LLM bindings.

    Call BEFORE importing ProcedureMem.memory.Memory.
    """
    if not reflector_key or reflector_key == "EMPTY":
        reflector_key = os.environ.get("OPENROUTER_API_KEY") or reflector_key
    _ensure_memp_path()
    os.environ.setdefault("OPENAI_API_KEY", "EMPTY")
    os.environ.setdefault("OPENAI_API_BASE", "https://openrouter.ai/api/v1")

    from ProcedureMem import llm_api  # type: ignore

    # Reset reflector telemetry on every install. Surfaces silent
    # reflector failures so honest-zero runs are distinguishable from
    # broken-reflector runs in the produce plan.
    _REFLECTOR_STATS["calls_total"] = 0
    _REFLECTOR_STATS["call_errors"] = 0
    _REFLECTOR_STATS["parse_errors"] = 0
    _REFLECTOR_STATS["successes"] = 0

    embedding_backend = os.environ.get("MEMP_EMBEDDING_BACKEND", "openai").strip().lower()
    local_embedding_model = (
        os.environ.get("MEMP_LOCAL_EMBEDDING_MODEL")
        or os.environ.get("MEMP_EMBEDDING_MODEL")
        or embedding_model.removeprefix("local_hf/")
    )
    emb_model_id = local_embedding_model.removeprefix("openrouter/")

    # Disk-backed cache for embeddings: skip OpenRouter calls when the
    # exact (model, text) pair has been embedded before. Lives under
    # the experiment artifacts dir so it persists across N-trial
    # reruns. Override via EMBEDDING_CACHE_DIR.
    cache_dir_env = os.environ.get("EMBEDDING_CACHE_DIR")
    cache_namespace = f"{embedding_backend}|{embedding_base}|{emb_model_id}"
    cache_hash = hashlib.sha256(cache_namespace.encode("utf-8")).hexdigest()[:12]
    cache_leaf = f"{emb_model_id.replace('/', '__')}__{cache_hash}"
    if cache_dir_env:
        emb_cache_dir = Path(cache_dir_env) / cache_leaf
    else:
        exp_root = Path(__file__).resolve().parents[3] / "experiments" / "4way-sbbench-pilot"
        emb_cache_dir = exp_root / "artifacts" / "embedding_cache" / cache_leaf

    from evolution.adapters._embedding_cache import CachedEmbeddings

    def _patched_get_embedding_model():
        if embedding_backend in {"sentence_transformers", "sentence-transformers", "local"}:
            base = _SentenceTransformerEmbeddings(local_embedding_model)
        else:
            from langchain_openai import OpenAIEmbeddings  # type: ignore

            base = OpenAIEmbeddings(
                model=emb_model_id,
                base_url=embedding_base,
                api_key=(
                    embedding_key
                    if (embedding_key or "").upper() not in {"", "EMPTY"}
                    else os.environ.get("OPENROUTER_API_KEY", embedding_key)
                ),
                default_headers={"HTTP-Referer": "https://localhost", "X-Title": "evo-skill-memp"},
            )
        return CachedEmbeddings(
            base,
            cache_dir=emb_cache_dir,
            model_id=cache_namespace,
        )

    llm_api.get_embedding_model = _patched_get_embedding_model  # type: ignore[attr-defined]

    from openai import OpenAI

    ref_timeout_s = float(
        os.environ.get(
            "EVO_MEMP_REFLECTOR_HTTP_TIMEOUT_S",
            os.environ.get("EVO_MEMP_SOLVER_HTTP_TIMEOUT_S", "120"),
        )
        or "120"
    )
    ref_client = OpenAI(api_key=reflector_key, base_url=reflector_base, timeout=ref_timeout_s)
    ref_model_id = reflector_model.removeprefix("openrouter/")
    # Coding-agent CLI reflector (codex/claude-code/hermes): route MemP's
    # reflector calls through AgentBackend instead of the HTTP OpenAI client.
    if reflector_model.split("/", 1)[0] in ("codex", "claude-code", "hermes"):
        from evolution.llm.backend import get_backend

        _agent_reflector = get_backend(reflector_model)
    else:
        _agent_reflector = None

    def _patched_llm(prompt, stop=None, model=None, is_string=False):
        """Route MemP's reflector call through OpenRouter Gemini."""
        import json as _json

        _REFLECTOR_STATS["calls_total"] += 1

        if isinstance(prompt, list):
            messages = prompt
        else:
            messages = [{"role": "user", "content": str(prompt)}]
        kwargs = {
            "model": ref_model_id,
            "messages": messages,
            "max_tokens": 4096,
            "temperature": 0.2,
        }
        if "gpt-oss" in ref_model_id.lower():
            if "openrouter.ai" in (reflector_base or "").lower():
                kwargs["extra_body"] = {
                    "reasoning": {
                        "effort": os.environ.get("EVO_OPENROUTER_REASONING_EFFORT", "low"),
                        "exclude": True,
                    }
                }
            else:
                kwargs["extra_body"] = {
                    "reasoning_effort": os.environ.get("EVO_LLM_REASONING_EFFORT", "low")
                }
        if stop:
            kwargs["stop"] = stop
        try:
            if _agent_reflector is not None:
                import asyncio

                sys_parts = [
                    str(m.get("content") or "") for m in messages if m.get("role") == "system"
                ]
                chat = [m for m in messages if m.get("role") != "system"]
                if not chat:
                    chat = [{"role": "user", "content": "\n\n".join(p for p in sys_parts if p)}]
                    sys_parts = []
                resp = asyncio.run(
                    _agent_reflector.complete(
                        chat, system="\n\n".join(p for p in sys_parts if p) or None
                    )
                )
                ans = resp.content or ""
            else:
                r = ref_client.chat.completions.create(**kwargs)
                ans = r.choices[0].message.content or ""
        except Exception as exc:
            _REFLECTOR_STATS["call_errors"] += 1
            logger.warning("memp reflector llm failed: %s", exc)
            return "" if is_string else {}
        if is_string:
            _REFLECTOR_STATS["successes"] += 1
            return ans
        cleaned = ans.strip()
        for fence in ("```json", "```JSON", "```py", "```python", "```"):
            if cleaned.startswith(fence):
                cleaned = cleaned[len(fence) :].lstrip("\n")
                break
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()
        try:
            parsed = _json.loads(cleaned)
        except Exception as exc:
            _REFLECTOR_STATS["parse_errors"] += 1
            logger.warning("memp reflector json-parse failed (%s); raw=%s", exc, ans[:200])
            return {}
        _REFLECTOR_STATS["successes"] += 1
        return parsed

    llm_api.llm = _patched_llm  # type: ignore[attr-defined]
    if hasattr(llm_api, "get_llm_response"):
        llm_api.get_llm_response = _patched_llm  # type: ignore[attr-defined]

    def _patched_adjust_llm(prompt, stop=None, model=None):
        return _patched_llm(prompt, stop=stop, model=model, is_string=True)

    from ProcedureMem import memory_adjust as _memory_adjust  # type: ignore

    _memory_adjust.llm = _patched_adjust_llm  # type: ignore[attr-defined]

    memory_mod = sys.modules.get("ProcedureMem.memory")
    if memory_mod is not None:
        if hasattr(memory_mod, "get_llm_response"):
            memory_mod.get_llm_response = _patched_llm  # type: ignore[attr-defined]
        if hasattr(memory_mod, "get_embedding_model"):
            memory_mod.get_embedding_model = _patched_get_embedding_model  # type: ignore[attr-defined]

    global _ORIGINAL_WORKFLOW_PROMPT
    from ProcedureMem import prompt_generator as _pg  # type: ignore

    memory_mod = sys.modules.get("ProcedureMem.memory")
    if _ORIGINAL_WORKFLOW_PROMPT is None:
        _ORIGINAL_WORKFLOW_PROMPT = _pg.generate_workflow_from_trajectory_prompt

    if os.environ.get("EVO_MEMP_CODE_AWARE_PROMPT", "1") != "0":

        def _patched_generate_workflow_from_trajectory_prompt(query, trajectory):
            template = "You are a helpful assistant."
            prompt = (
                "You are given a query and a trajectory of how a coding agent solved "
                "it. Each step has thought, action, observation. Write a workflow "
                "that another agent could follow to solve a similar coding task.\n\n"
                "REQUIREMENTS:\n"
                "- Identify the LIBRARIES, APIS, FILE FORMATS, and SCHEMAS the "
                "  successful trajectory relied on. Name them explicitly.\n"
                "- Quote the SHORT key code patterns (<= 10 lines each) that the "
                "  trajectory used: imports, schema definitions, the core call "
                "  signature, the validation/test invocation. Use a fenced block.\n"
                "- Describe the order of operations as numbered steps with the "
                "  function/method that each step calls.\n"
                "- Mention pitfalls observed in the trajectory (errors, retries, "
                "  edge cases) and how the agent resolved them.\n"
                "- DO NOT fabricate code that wasn't in the trajectory. If a "
                "  detail isn't in the trajectory, say 'not specified in trajectory'.\n"
                "- Keep the workflow under 2500 chars total.\n\n"
                f"Query:\n{query}\n\n"
                f"Trajectory:\n{trajectory}\n\n"
                "Output the workflow only, no preamble or explanation:"
            )
            return [
                {"role": "system", "content": template},
                {"role": "user", "content": prompt},
            ]

        workflow_prompt = _patched_generate_workflow_from_trajectory_prompt
    else:
        workflow_prompt = _ORIGINAL_WORKFLOW_PROMPT

    _pg.generate_workflow_from_trajectory_prompt = workflow_prompt
    if memory_mod is not None and hasattr(memory_mod, "generate_workflow_from_trajectory_prompt"):
        memory_mod.generate_workflow_from_trajectory_prompt = workflow_prompt  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Step-env solver loop (one train episode → trajectory)
# ---------------------------------------------------------------------------

_SOLVER_SYSTEM = (
    """\
You are an autonomous Python coding agent solving the task below in a
sandboxed workspace. You may use these actions one at a time, exactly
one per response (no markdown, no JSON):

  list_files                    — list workspace contents
  read_file <path>              — read up to 16KB of a file
  write_solution <python code>  — write _solve.py (replaces prior)
  write_file <path>             — write a durable workspace file; put the
                                  file content on following lines
  run_tests                     — run pytest, reports pass rate
  done <answer>                 — end the episode

Workflow: explore (list_files, read_file), write a complete
self-contained _solve.py using the workspace files, or use write_file for
requested durable artifacts such as output/*.json, output/*.yaml, output/*.csv,
output/*.txt, and output/*.md. Then run_tests, iterate on failures, and
``done`` when satisfied. Each action's observation
returns to you in the next turn. Keep responses short — one action.

Output contract: tests verify required deliverables under the workspace
``output/`` directory. If the task asks for ``foo.pdf``, your solution
must write ``output/foo.pdf`` from workspace root. If it asks for
``redacted/paper1.pdf``, write ``output/redacted/paper1.pdf``. For textual
artifacts, prefer `write_file output/<name>` followed by the exact content.
"""
    + PROC_MEMORY_DONE_SUMMARY
)

_SPECIAL_TOKEN_RE = re.compile(r"<\|[^>]*\|>")
_ACTION_PREFIX_RE = re.compile(r"^\s*(?:action|next action)\s*:\s*", re.IGNORECASE)
_PYTHON_CODE_PREFIX_RE = re.compile(r"^\s*<\s*python\s+code\s*", re.IGNORECASE)
_TOOL_CODE_PREFIX_RE = re.compile(
    r"^\s*(?:assistant)?(?:analysis|commentary)?\s*(?:to=)?container\.exec\s+code\s*",
    re.IGNORECASE,
)
_VALID_STEP_ACTIONS = {
    "list_files",
    "read_file",
    "write_solution",
    "write_file",
    "run_tests",
    "done",
}
_COMMON_PATH_EXTENSIONS = (
    ".py",
    ".md",
    ".txt",
    ".json",
    ".csv",
    ".yaml",
    ".yml",
    ".pdf",
    ".docx",
    ".xlsx",
    ".sql",
    ".toml",
    ".html",
    ".xml",
    ".png",
    ".jpg",
    ".jpeg",
    ".zip",
    ".parquet",
)


def _strip_outer_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    body = "\n".join(lines[1:])
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def _extract_first_fenced_block(text: str) -> str:
    match = re.search(
        r"```[A-Za-z0-9_.+-]*\n(.*?)\n```",
        text or "",
        flags=re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _extract_artifact_path(text: str) -> str:
    match = re.search(
        r"(?<![A-Za-z0-9_./-])(output/[A-Za-z0-9_./-]+\.(?:json|ya?ml|csv|txt|md|html|xml))",
        text or "",
        flags=re.IGNORECASE,
    )
    return match.group(1).strip("`'\".,:;()[]{}") if match else ""


def _extract_unfenced_artifact_body(text: str, path: str) -> str:
    suffix = Path(path).suffix.lower()
    lines = (text or "").splitlines()
    start = None
    if suffix in {".yaml", ".yml"}:
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(("apiVersion:", "---")):
                start = i
                break
    elif suffix == ".json":
        for i, line in enumerate(lines):
            if line.lstrip().startswith(("{", "[")):
                start = i
                break
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if body and re.match(
            r"^(?:Step\s+\d+|Verify|Important|Now\s+|Make sure|Use\s+|done\.?$)",
            stripped,
            flags=re.IGNORECASE,
        ):
            break
        body.append(line)
    return "\n".join(body).strip()


def _maybe_file_write_action(text: str) -> str:
    path = _extract_artifact_path(text)
    if not path:
        return ""
    body = _extract_first_fenced_block(text)
    if not body:
        body = _extract_unfenced_artifact_body(text, path)
    if not body:
        return ""
    return f"write_file {path}\n{body}"


def _first_path_arg(rest: str) -> str:
    """Extract one workspace path from a model action argument."""
    text = rest.strip().lstrip("(:=").strip()
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    try:
        parts = shlex.split(first_line, posix=True)
    except ValueError:
        parts = first_line.split()
    if not parts:
        return ""
    token = parts[0].strip().strip("`'\"")
    token = token.lstrip("(<[{`'\"")
    token = re.sub(r"[)>}\]`'\".,;:]+$", "", token)
    if not token:
        return ""

    # Small open-source models sometimes concatenate a path and prose
    # ("solution.pyWe need..."). Keep the intended file name when the boundary
    # is obvious; otherwise reject the path rather than letting it hit the FS.
    lowered = token.lower()
    for ext in sorted(_COMMON_PATH_EXTENSIONS, key=len, reverse=True):
        idx = lowered.find(ext)
        if idx < 0:
            continue
        end = idx + len(ext)
        if end < len(token):
            suffix = token[end:]
            if suffix[:1].isupper() or suffix.lower().startswith(("we", "let", "read")):
                token = token[:end]
        break

    if len(token) > 240 or any(ch in token for ch in "\r\n\x00"):
        return ""
    if token in {"|", "&&", ";", ">", "<"}:
        return ""
    return token


def _action_from_shell_command(command: str) -> str:
    command = command.strip()
    if not command:
        return ""
    lowered = command.lower()
    if lowered in {"ls", "ls ."} or lowered.startswith(("ls ", "find ")):
        return "list_files"
    if lowered.startswith("cat "):
        target = _first_path_arg(command.split(None, 1)[1])
        return "read_file " + target if target else ""
    sed_match = re.search(
        r"\bsed\s+-n\s+(?:'[^']+'|\"[^\"]+\"|\S+)\s+(.+)$",
        command,
        flags=re.IGNORECASE,
    )
    if sed_match:
        target = _first_path_arg(sed_match.group(1))
        return "read_file " + target if target else ""
    return ""


def _action_from_embedded_shell_json(text: str) -> str:
    """Recover trivial shell-tool hallucinations into the MemP action grammar."""
    first = text.find("{")
    last = text.rfind("}")
    if first < 0 or last <= first:
        return ""
    blob = text[first : last + 1]
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    cmd = parsed.get("cmd")
    command = ""
    if isinstance(cmd, list):
        parts = [str(x) for x in cmd]
        if len(parts) >= 3 and parts[0] in {"bash", "sh"} and parts[1] in {"-lc", "-c"}:
            command = parts[2]
        else:
            command = " ".join(parts)
    elif isinstance(cmd, str):
        command = cmd
    return _action_from_shell_command(command)


def _normalize_candidate_action(candidate: str, *, allow_embedded: bool) -> str:
    candidate = _SPECIAL_TOKEN_RE.sub("", candidate).strip().strip("`")
    candidate = _TOOL_CODE_PREFIX_RE.sub("", candidate).strip()
    candidate = _PYTHON_CODE_PREFIX_RE.sub("", candidate).strip()
    candidate = re.sub(r"^(?:[-*]|\d+[.)])\s*", "", candidate).strip()
    candidate = _ACTION_PREFIX_RE.sub("", candidate).strip()
    if not candidate:
        return ""

    lowered = candidate.lower()
    if lowered in {"ls", "ls .", "find . -maxdepth 2 -type f", "find . -type f"}:
        return "list_files"
    if lowered == "list_files":
        return "list_files"
    if lowered == "run_tests" or re.fullmatch(
        r"(?:we need to\s+)?run\s+(?:the\s+)?tests?\.?", lowered
    ):
        return "run_tests"
    if lowered.startswith("cat "):
        target = _first_path_arg(candidate.split(None, 1)[1])
        return "read_file " + target if target else ""
    if lowered.startswith("read_file"):
        target = _first_path_arg(candidate[len("read_file") :])
        return "read_file " + target if target else ""
    if lowered.startswith("done"):
        return "done" + candidate[4:]
    if lowered.startswith("write_solution"):
        return "write_solution" + candidate[len("write_solution") :]
    if lowered.startswith("write_file"):
        rest = candidate[len("write_file") :].strip()
        return "write_file " + rest if rest else ""

    file_write = _maybe_file_write_action(candidate)
    if file_write:
        return file_write

    if not allow_embedded:
        return ""

    shell_action = _action_from_embedded_shell_json(candidate)
    if shell_action:
        return shell_action

    if re.search(r"(?<![A-Za-z_])list_files(?![A-Za-z_])", candidate, re.IGNORECASE):
        return "list_files"
    if re.search(r"(?<![A-Za-z_])run_tests(?![A-Za-z_])", candidate, re.IGNORECASE):
        return "run_tests"
    if re.search(r"\brun\s+(?:the\s+)?tests?\b", candidate, re.IGNORECASE):
        return "run_tests"
    match = re.search(r"(?<![A-Za-z_])read_file\s*(.*)$", candidate, re.IGNORECASE)
    if match:
        target = _first_path_arg(match.group(1))
        return "read_file " + target if target else ""
    match = re.search(r"(?<![A-Za-z_])cat\s+(.+)$", candidate, re.IGNORECASE)
    if match:
        target = _first_path_arg(match.group(1))
        return "read_file " + target if target else ""
    file_write = _maybe_file_write_action(candidate)
    if file_write:
        return file_write
    return ""


def clean_solver_action(raw: str) -> str:
    """Normalize one model response into the MemP step-env action grammar."""
    text = _SPECIAL_TOKEN_RE.sub("", raw or "").replace("\r\n", "\n").strip()
    text = _TOOL_CODE_PREFIX_RE.sub("", text).strip()
    text = _PYTHON_CODE_PREFIX_RE.sub("", text).strip()
    text = re.sub(r"</\s*python\s*>\s*$", "", text, flags=re.IGNORECASE).strip()
    text = _strip_outer_fence(text)
    text = _PYTHON_CODE_PREFIX_RE.sub("", text).strip()
    text = _ACTION_PREFIX_RE.sub("", text).strip()
    if not text:
        return ""

    if re.match(r"^(import|from|def|class)\s", text):
        return "write_solution " + text

    file_write = _maybe_file_write_action(text)
    if file_write:
        return file_write

    whole = _normalize_candidate_action(text, allow_embedded=False)
    if whole:
        return whole

    for line in text.splitlines():
        normalized = _normalize_candidate_action(line, allow_embedded=False)
        if normalized:
            return normalized

    embedded = _normalize_candidate_action(text, allow_embedded=True)
    if embedded:
        return embedded

    if "\n" in text:
        return "write_solution " + text
    return ""


def _solver_chat(
    messages: list[dict],
    *,
    base: str,
    key: str,
    model_id: str,
    max_tokens: int = 4096,
    timeout_s: float | None = None,
) -> tuple[str, dict]:
    """One round-trip to the solver. Returns (text, usage_dict)."""
    from openai import OpenAI

    client = OpenAI(api_key=key or "EMPTY", base_url=base, timeout=timeout_s)
    extra_body = {}
    model_l = model_id.lower()
    if "qwen3" in model_l:
        extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
    elif "gpt-oss" in model_l:
        if "openrouter.ai" in (base or "").lower():
            extra_body = {
                "reasoning": {
                    "effort": os.environ.get("EVO_OPENROUTER_REASONING_EFFORT", "low"),
                    "exclude": True,
                }
            }
        else:
            extra_body = {"reasoning_effort": os.environ.get("EVO_LLM_REASONING_EFFORT", "low")}
    t0 = time.time()
    r = client.chat.completions.create(
        model=model_id,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.2,
        extra_body=extra_body or None,
    )
    dt_ms = (time.time() - t0) * 1000
    choice = r.choices[0]
    message = choice.message
    content = getattr(message, "content", None) or ""
    content_source = "content"
    if not content:
        for attr in ("reasoning_content", "reasoning"):
            value = getattr(message, attr, None)
            if value:
                content = str(value)
                content_source = attr
                break
    if not content and hasattr(message, "model_dump"):
        try:
            dumped = message.model_dump()
        except Exception:
            dumped = {}
        for key in ("reasoning_content", "reasoning", "text"):
            value = dumped.get(key) if isinstance(dumped, dict) else None
            if value:
                content = str(value)
                content_source = key
                break
        if not content and isinstance(dumped, dict) and dumped.get("tool_calls"):
            content = json.dumps(dumped.get("tool_calls"), ensure_ascii=False)
            content_source = "tool_calls"
    usage = getattr(r, "usage", None)
    usage_d = {
        "tokens_in": int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
        "tokens_out": int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
        "latency_ms": round(dt_ms, 1),
        "model_id": model_id,
        "content_source": content_source,
        "empty_content": not bool(content),
    }
    return content or "", usage_d


def run_episode(
    env: TaskEnv,
    *,
    task_instruction: str,
    retrieved_workflows: list[str],
    solver_base: str,
    solver_key: str,
    solver_model_id: str,
    max_steps: int = 12,
    timeout_s: float | None = None,
) -> dict:
    """Drive the agent through the step env; return trajectory dict."""
    started = time.monotonic()
    deadline = started + float(timeout_s) if timeout_s else None
    obs = env.reset()
    messages: list[dict] = [{"role": "system", "content": _SOLVER_SYSTEM}]
    user_msg = f"Task instruction:\n{task_instruction[:_INSTR_CHARS]}\n\n"
    if retrieved_workflows:
        user_msg += "Retrieved procedures (use as guidance):\n"
        for i, w in enumerate(retrieved_workflows, 1):
            user_msg += f"--- procedure {i} ---\n{w[:_WORKFLOW_CHARS]}\n"
        user_msg += "---\n\n"
    user_msg += f"Initial observation:\n{obs}\n\nYour next action:"
    messages.append({"role": "user", "content": user_msg})

    trajectory: list[dict] = []
    final_reward = 0.0
    per_turn_usage: list[dict] = []
    solver_error: str | None = None
    verifier_trace: dict = {}
    wrote_artifact = False
    ran_tests = False
    empty_action_streak = 0
    invalid_action_streak = 0
    for step_i in range(max_steps):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            solver_error = f"TimeoutError('MemP episode timed out after {timeout_s}s')"
            logger.warning("memp episode timeout before step %s", step_i)
            break
        call_timeout_s = float(
            os.environ.get(
                "EVO_MEMP_SOLVER_HTTP_TIMEOUT_S",
                str(min(120.0, max(1.0, remaining)) if remaining is not None else 120.0),
            )
        )
        if remaining is not None:
            call_timeout_s = max(1.0, min(call_timeout_s, remaining))
        try:
            action, usage = _solver_chat(
                messages,
                base=solver_base,
                key=solver_key,
                model_id=solver_model_id,
                timeout_s=call_timeout_s,
            )
            per_turn_usage.append(usage)
        except Exception as exc:
            solver_error = repr(exc)
            logger.warning("memp solver step failed: %s", exc)
            break
        action_clean = clean_solver_action(action)
        if not action_clean:
            empty_action_streak += 1
            observation = (
                "Empty action. Reply with exactly one valid action: list_files, "
                "read_file <path>, write_solution <python code>, "
                "write_file <path> followed by file content, run_tests, or done <answer>."
            )
            if empty_action_streak >= int(os.environ.get("EVO_MEMP_EMPTY_ACTION_LIMIT", "3")):
                solver_error = f"empty_action_streak:{empty_action_streak}"
                trajectory.append(
                    {
                        "step": step_i,
                        "raw_action": action[:_ACTION_CHARS],
                        "action": "",
                        "observation": "Empty action limit reached.",
                        "done": False,
                        "empty_action_limit_reached": True,
                    }
                )
                break
            trajectory.append(
                {
                    "step": step_i,
                    "raw_action": action[:_ACTION_CHARS],
                    "action": "",
                    "observation": observation,
                    "done": False,
                    "empty_action": True,
                }
            )
            messages.append({"role": "assistant", "content": action[:_ACTION_CHARS] or "(empty)"})
            messages.append({"role": "user", "content": observation})
            continue
        else:
            empty_action_streak = 0
        verb = action_clean.split(None, 1)[0].lower() if action_clean.strip() else ""
        if action_clean and verb not in _VALID_STEP_ACTIONS:
            invalid_action_streak += 1
            observation = (
                "Invalid action. Use exactly one of: list_files, read_file <path>, "
                "write_solution <python code>, write_file <path>\\n<content>, "
                "run_tests, done <answer>."
            )
            trajectory.append(
                {
                    "step": step_i,
                    "raw_action": action[:_ACTION_CHARS],
                    "action": action_clean[:_ACTION_CHARS],
                    "observation": observation,
                    "done": False,
                    "invalid_action": True,
                }
            )
            messages.append({"role": "assistant", "content": action[:_ACTION_CHARS]})
            messages.append({"role": "user", "content": observation})
            if invalid_action_streak >= int(os.environ.get("EVO_MEMP_INVALID_ACTION_LIMIT", "3")):
                solver_error = f"invalid_action_streak:{invalid_action_streak}"
                break
            continue
        invalid_action_streak = 0
        if action_clean.lower().startswith("done") and not (wrote_artifact and ran_tests):
            observation = (
                "done rejected: write_solution or write_file, then run_tests, are required before done. "
                "Continue with a valid action."
            )
            trajectory.append(
                {
                    "step": step_i,
                    "raw_action": action[:_ACTION_CHARS],
                    "action": action_clean[:_ACTION_CHARS],
                    "observation": observation,
                    "done": False,
                    "rejected_premature_done": True,
                }
            )
            messages.append({"role": "assistant", "content": action[:_ACTION_CHARS]})
            messages.append({"role": "user", "content": observation})
            continue
        result = env.step(action_clean)
        if verb in {"write_solution", "write_file"}:
            wrote_artifact = True
        elif verb == "run_tests":
            ran_tests = True
        trajectory.append(
            {
                "step": step_i,
                "raw_action": action[:_ACTION_CHARS],
                "action": action_clean[:_ACTION_CHARS],
                "observation": result.observation[:_OBS_CHARS],
                "done": result.done,
                "done_answer": (action_clean[4:].strip() if result.done else "")[:_ACTION_CHARS],
            }
        )
        messages.append({"role": "assistant", "content": action[:_ACTION_CHARS]})
        messages.append({"role": "user", "content": result.observation[:_MSG_CHARS]})
        if result.done:
            final_reward = result.reward
            break

    if env.total_tests == 0:
        forced_steps: list[dict] = []
        if not (env.workspace / "_solve.py").is_file():
            fallback = '"""MemP did not produce a solution before the step budget expired."""\n'
            wrote = env.step(f"write_solution {fallback}")
            forced_steps.append(
                {
                    "step": len(trajectory),
                    "action": "write_solution <adapter no-op after step budget>",
                    "observation": wrote.observation[:_OBS_CHARS],
                    "done": wrote.done,
                    "forced_by_adapter": True,
                }
            )
        measured = env.step("run_tests")
        forced_steps.append(
            {
                "step": len(trajectory),
                "action": "run_tests <adapter measurement after step budget>",
                "observation": measured.observation[:_OBS_CHARS],
                "done": measured.done,
                "forced_by_adapter": True,
            }
        )
        verifier_trace["adapter_forced_measurement"] = {
            "steps": forced_steps,
            "last_verifier_trace": env.last_verifier_trace or {},
        }
    elif env.last_verifier_trace:
        verifier_trace["last_verifier_trace"] = env.last_verifier_trace

    return {
        "task_id": env.task_id,
        "instruction": task_instruction[:_QUERY_CHARS],
        "steps": trajectory,
        "final_reward": final_reward,
        "final_pass_rate": env.last_pass_rate,
        "total_tests": env.total_tests,
        "per_turn_usage": per_turn_usage,
        "solver_error": solver_error,
        "native_contract": {
            "query": task_instruction[:_QUERY_CHARS],
            "trajectory": trajectory,
            "reward": final_reward,
            "workflow_count": len(retrieved_workflows),
        },
        "verifier_trace": verifier_trace,
    }


def _trajectory_text(t: dict) -> str:
    parts = []
    for s in t.get("steps", []):
        parts.append(
            f"step {s['step']}: action={s['action']!r}\n"
            f"  observation={s['observation'][:_TRAJ_OBS_CHARS]!r}"
        )
        if s.get("done_answer"):
            parts.append(f"  done_answer={s['done_answer'][:_TRAJ_OBS_CHARS]!r}")
    return "\n".join(parts)


def _build_memory(
    memory_dir: Path,
    trajectories: list[dict],
    base_workflows: list[tuple[str, str]] | None = None,
) -> tuple[object, dict[str, int]]:
    """Build MemP Memory from trajectories: process + reflect, persist."""
    _ensure_memp_path()
    from langchain_core.documents import Document  # type: ignore
    from ProcedureMem.memory import Memory  # type: ignore

    memory_dir.mkdir(parents=True, exist_ok=True)
    mem = Memory(
        is_cold_start=False,
        policy={"build": "direct", "retrieve": "facts", "update": "replace"},
        memory_dir=str(memory_dir),
        retrieve_num=3,
        memory_size=1000,
    )

    counts = {
        "base_docs": 0,
        "build_in": 0,
        "build_docs": 0,
        "reflect_in": 0,
        "reflect_adjusted": 0,
    }

    for name, body in base_workflows or []:
        mem.documents.append(
            Document(
                page_content=body,
                metadata={
                    "source": f"base_skill:{name}",
                    "workflow": body,
                    "task_id": name,
                    "seed_policy": "source_trace_task_skill_union_v1",
                },
            )
        )
        counts["base_docs"] += 1

    new_documents = []
    for t in trajectories:
        item = {
            "source": t["task_id"],
            "query": t["instruction"][:_QUERY_CHARS],
            "trajectory": _trajectory_text(t)[:_TRAJ_CHARS],
            "reward": t.get("final_reward", 0.0),
            "task_id": t["task_id"],
        }
        counts["build_in"] += 1
        try:
            doc = mem.process_trajectory_item(item)
            if doc is not None:
                meta = getattr(doc, "metadata", None)
                if isinstance(meta, dict):
                    meta.setdefault("reward", item["reward"])
                    meta.setdefault("final_pass_rate", t.get("final_pass_rate", item["reward"]))
                    meta.setdefault("total_tests", t.get("total_tests", 0))
                new_documents.append(doc)
                counts["build_docs"] += 1
        except Exception as exc:
            logger.warning(
                "memp Memory.process_trajectory_item failed for %s: %s", t["task_id"], exc
            )
    mem.documents.extend(new_documents)
    try:
        mem._save_documents()
    except Exception as exc:
        logger.warning("memp Memory._save_documents failed: %s", exc)

    for t in trajectories:
        counts["reflect_in"] += 1
        query = t["instruction"][:_QUERY_CHARS]
        traj_text = _trajectory_text(t)[:_TRAJ_CHARS]
        reward = t.get("final_reward", 0.0)
        workflow = ""
        try:
            results = mem.retrieve(str(query)) or []
        except Exception as exc:
            logger.warning("memp retrieve during reflect failed for %s: %s", t["task_id"], exc)
            results = []
        for r in results:
            doc = r[0] if isinstance(r, tuple) else r
            wf = (getattr(doc, "metadata", {}) or {}).get("workflow") or ""
            if wf:
                workflow = str(wf)
                break
        try:
            mem.process_trajectory_item_reflect(traj_text, reward, workflow)
            if not reward and workflow:
                counts["reflect_adjusted"] += 1
        except Exception as exc:
            logger.warning(
                "memp process_trajectory_item_reflect failed for %s: %s", t["task_id"], exc
            )

    try:
        mem._save_documents()
    except Exception as exc:
        logger.warning("memp Memory._save_documents (post-reflect) failed: %s", exc)

    return mem, counts


def _load_memory_from_dir(memory_dir: Path, *, retrieve_num: int = 3) -> object:
    """Load MemP's persisted ``documents.json`` into a live Memory object."""
    _ensure_memp_path()
    from langchain_core.documents import Document  # type: ignore
    from ProcedureMem.memory import Memory  # type: ignore

    mem = Memory(
        is_cold_start=False,
        policy={"build": "direct", "retrieve": "facts", "update": "replace"},
        memory_dir=str(memory_dir),
        retrieve_num=retrieve_num,
        memory_size=1000,
    )
    docs_path = _documents_json_path(memory_dir)
    if docs_path.is_file():
        docs_data = json.loads(docs_path.read_text(encoding="utf-8"))
        mem.documents = [Document(**d) for d in docs_data]
    if mem.documents:
        mem._save_documents()
    return mem


def _count_unique_workflows(mem: object) -> int:
    docs = getattr(mem, "documents", []) or []
    seen: set[str] = set()
    for doc in docs:
        meta = getattr(doc, "metadata", {}) or {}
        wf = (meta.get("workflow") or "").strip()
        if wf and wf not in seen:
            seen.add(wf)
    return len(seen)


def produce(
    *,
    pilot: PilotManifest,
    solver_model: str,
    solver_base: str,
    solver_key: str,
    reflector_model: str,
    reflector_base: str,
    reflector_key: str,
    embedding_model: str,
    embedding_base: str,
    embedding_key: str,
    task_instructions: dict[str, str],
    task_envs: dict[str, TaskEnv],
    memp_home: Path = MEMP_HOME,
    max_steps_per_task: int = 12,
    base_workflows: list[tuple[str, str]] | None = None,
) -> MempProducePlan:
    """Drive train episodes, build Memory, persist for the test runner."""
    plan = MempProducePlan()
    started_at = time.time()

    if memp_home.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        archive = memp_home.with_name(f"{memp_home.name}_{ts}")
        if archive.exists():
            plan.notes.append(f"memp: refusing to overwrite archive {archive}")
            plan.skipped = True
            plan.skip_reason = "archive collision"
            return plan
        memp_home.rename(archive)
        plan.notes.append(f"memp: archived prior MEMP_HOME → {archive.name}")
    memp_home.mkdir(parents=True, exist_ok=True)

    install_monkeypatches(
        embedding_model=embedding_model,
        embedding_base=embedding_base,
        embedding_key=embedding_key,
        reflector_model=reflector_model,
        reflector_base=reflector_base,
        reflector_key=reflector_key,
    )
    plan.notes.append(
        f"memp: monkey-patches installed (embedding={embedding_model}, reflector={reflector_model})"
    )

    served_solver = solver_model.split("/", 1)[-1] if "/" in solver_model else solver_model

    trajectories: list[dict] = []
    for t in pilot.train:
        env = task_envs.get(t.id)
        if env is None:
            plan.notes.append(f"memp: train task {t.id} missing TaskEnv; skipping")
            continue
        instr = task_instructions.get(t.id, "")
        try:
            traj = run_episode(
                env,
                task_instruction=instr,
                retrieved_workflows=[body for _, body in base_workflows or []],
                solver_base=solver_base,
                solver_key=solver_key,
                solver_model_id=solver_model.removeprefix("openrouter/")
                if solver_model.startswith("openrouter/")
                else served_solver,
                max_steps=max_steps_per_task,
            )
        except Exception as exc:
            logger.warning("memp episode failed for %s: %s", t.id, exc)
            plan.attempts.append({"task_id": t.id, "ok": False, "error": repr(exc)[:300]})
            continue
        trajectories.append(traj)
        # Sum tokens and latency across solver turns for this task.
        total_in = sum(u.get("tokens_in", 0) for u in traj.get("per_turn_usage", []))
        total_out = sum(u.get("tokens_out", 0) for u in traj.get("per_turn_usage", []))
        total_lat = sum(u.get("latency_ms", 0.0) for u in traj.get("per_turn_usage", []))
        plan.attempts.append(
            {
                "task_id": t.id,
                "ok": True,
                "final_reward": traj["final_reward"],
                "final_pass_rate": traj["final_pass_rate"],
                "total_tests": traj["total_tests"],
                "n_steps": len(traj["steps"]),
                "tokens_in_total": total_in,
                "tokens_out_total": total_out,
                "latency_ms_total": round(total_lat, 1),
            }
        )

    plan.n_train_trajectories = len(trajectories)
    if not trajectories:
        plan.notes.append("memp: no train trajectories produced; Memory will be empty.")
        plan.runtime_seconds = round(time.time() - started_at, 1)
        return plan

    memory_dir = memp_home / "memory"
    try:
        mem, build_counts = _build_memory(
            memory_dir,
            trajectories,
            base_workflows=base_workflows,
        )
        plan.memory_dir = str(memory_dir)
        plan.n_unique_workflows = _count_unique_workflows(mem)
        plan.n_reflect_adjusted = build_counts["reflect_adjusted"]
        plan.notes.append(
            f"memp: Memory built from {len(trajectories)} trajectories — "
            f"{build_counts['build_docs']} docs created, "
            f"{plan.n_unique_workflows} unique workflows, "
            f"{plan.n_reflect_adjusted} adjusted via process_trajectory_item_reflect"
        )
    except Exception as exc:
        plan.skipped = True
        plan.skip_reason = f"Memory build failed: {exc!r}"[:300]
        plan.runtime_seconds = round(time.time() - started_at, 1)
        return plan

    # Surface reflector telemetry. The patched LLM closure swallows
    # exceptions and JSON-parse errors (returning empty values) so MemP's
    # internal flow doesn't crash mid-build, but those silent failures
    # masquerade as "no useful workflow". Copy the per-call counters into
    # the produce plan and hard-skip if every reflector call failed —
    # that's a broken reflector, not an honest empty memory.
    plan.n_reflector_calls = _REFLECTOR_STATS["calls_total"]
    plan.n_reflector_call_errors = _REFLECTOR_STATS["call_errors"]
    plan.n_reflector_parse_errors = _REFLECTOR_STATS["parse_errors"]
    plan.n_reflector_successes = _REFLECTOR_STATS["successes"]
    plan.notes.append(
        f"memp: reflector stats — {plan.n_reflector_calls} calls, "
        f"{plan.n_reflector_successes} ok, "
        f"{plan.n_reflector_call_errors} llm errors, "
        f"{plan.n_reflector_parse_errors} parse errors"
    )
    if plan.n_reflector_calls > 0 and plan.n_reflector_successes == 0:
        plan.skipped = True
        plan.skip_reason = (
            f"reflector entirely non-functional: {plan.n_reflector_calls} calls, "
            f"all failed ({plan.n_reflector_call_errors} llm, "
            f"{plan.n_reflector_parse_errors} parse). Memory may be empty/malformed."
        )

    plan.runtime_seconds = round(time.time() - started_at, 1)
    return plan


def _bundle_to_trajectory(bundle: dict) -> dict:
    passed = int(bundle.get("passed", 0) or 0)
    total = int(bundle.get("total", 0) or 0)
    reward = 1.0 if total > 0 and passed == total else 0.0
    code = str(bundle.get("code") or "")
    obs_parts = [
        "## Solver stdout/stderr",
        str(bundle.get("stdout") or ""),
        "## Pytest/verifier output",
        str(bundle.get("pytest_full") or ""),
        "## Workspace file previews",
        json.dumps(bundle.get("workspace_file_previews") or [], ensure_ascii=False, indent=2),
        "## Produced artifacts",
        json.dumps(bundle.get("produced_artifacts") or [], ensure_ascii=False, indent=2),
    ]
    obs = "\n".join(obs_parts)
    return {
        "task_id": bundle.get("task_id", ""),
        "instruction": str(bundle.get("instruction") or "")[:_QUERY_CHARS],
        "steps": [
            {
                "step": 0,
                "action": code[:_ACTION_CHARS],
                "observation": obs[:_OBS_CHARS],
            }
        ],
        "final_reward": reward,
        "final_pass_rate": float(bundle.get("pass_rate", 0.0) or 0.0),
        "total_tests": total,
    }


def _strip_skill_md_envelope(text: str) -> str:
    match = re.match(r"^---\s*\n.*?\n---\s*\n(.*)$", text.strip(), flags=re.DOTALL)
    return (match.group(1) if match else text).strip()


def _workflow_candidate_from_metadata(
    meta: dict,
    *,
    index: int,
    selected_by: str,
    score: float | None = None,
) -> dict | None:
    source = str(meta.get("source") or "")
    if source.startswith("base_skill:"):
        return None
    workflow = _strip_skill_md_envelope(str(meta.get("workflow") or "").strip())
    if not workflow:
        return None
    candidate = {
        "index": index,
        "source": source,
        "query": str(meta.get("query") or ""),
        "workflow": workflow,
        "selected_by": selected_by,
    }
    if score is not None:
        candidate["score"] = score
    return candidate


def render_memory_to_skill(
    *,
    memory_dir: Path,
    skill_id: str,
    query: str | None = None,
) -> tuple[str, dict]:
    """Lossy bridge: MemP stored/retrieved workflow -> canonical SKILL.md snapshot.

    This export intentionally does not quality-filter MemP's workflows. Native
    MemP evaluation uses persisted Memory directly; the SKILL.md bridge is only
    a secondary compatibility artifact and should expose bridge limitations in
    metadata instead of hiding them with harness-side heuristics.
    """
    memory_dir = Path(memory_dir)
    selected: dict | None = None
    candidates: list[dict] = []
    base_workflow_count = 0
    bridge_warnings: list[str] = []
    docs_path = _documents_json_path(memory_dir)
    if docs_path.is_file():
        try:
            docs = json.loads(docs_path.read_text(encoding="utf-8"))
            if isinstance(docs, list):
                for idx, doc in enumerate(docs):
                    meta = (doc or {}).get("metadata") or {}
                    if str(meta.get("source") or "").startswith("base_skill:"):
                        base_workflow_count += 1
                    candidate = _workflow_candidate_from_metadata(
                        meta,
                        index=idx,
                        selected_by="document_order",
                    )
                    if candidate:
                        candidates.append(candidate)
        except Exception as exc:
            logger.warning("memp render_memory_to_skill parse failed: %s", exc)
            bridge_warnings.append(f"documents_json_parse_failed: {exc!r}"[:300])

    if query and candidates:
        try:
            mem = _load_memory_from_dir(memory_dir, retrieve_num=1)
            retrieved = mem.retrieve(query[:_QUERY_CHARS]) or []
            for ridx, item in enumerate(retrieved):
                score = None
                doc = item
                if isinstance(item, tuple):
                    doc = item[0]
                    try:
                        score = float(item[1])
                    except Exception:
                        score = None
                meta = getattr(doc, "metadata", {}) or {}
                candidate = _workflow_candidate_from_metadata(
                    meta,
                    index=ridx,
                    selected_by="native_retrieve",
                    score=score,
                )
                if candidate:
                    selected = candidate
                    break
        except Exception as exc:
            logger.warning("memp bridge retrieve failed: %s", exc)
            bridge_warnings.append(f"native_retrieve_failed: {exc!r}"[:300])

    if selected is None and candidates:
        # MemP appends newly built documents; choosing the latest non-base
        # document is a transparent export fallback when no task query exists.
        selected = candidates[-1]

    workflow = str((selected or {}).get("workflow") or "")
    workflow_found = bool(workflow.strip())
    if not workflow:
        workflow = "(no workflow retrieved from MemP memory store)"
        bridge_warnings.append("no_non_base_workflow_in_memory")
    if len(workflow) > _WORKFLOW_CHARS:
        bridge_warnings.append(f"workflow_truncated_to_{_WORKFLOW_CHARS}_chars")
    header = (
        f"---\n"
        f"name: {skill_id}\n"
        f"description: MemP procedural memory bridge (lossy)\n"
        f"metadata:\n"
        f"  bridge: memp_render_memory_to_skill\n"
        f'  lossy: "true"\n'
        f"---\n\n"
    )
    body = header + f"# Distilled workflow\n\n{workflow[:_WORKFLOW_CHARS]}\n"
    return body, {
        "editor": "memp",
        "lossy_bridge": True,
        "memory_dir": str(memory_dir),
        "workflow_found": workflow_found,
        "workflow_source": str((selected or {}).get("source") or ""),
        "workflow_query": str((selected or {}).get("query") or ""),
        "workflow_index": int((selected or {}).get("index") or 0) if selected else None,
        "selection_policy": str((selected or {}).get("selected_by") or "none"),
        "candidate_count": len(candidates),
        "base_workflow_count": base_workflow_count,
        "bridge_warnings": bridge_warnings,
    }


def edit_skill_from_traces(
    *,
    skill_id: str,
    skill_body: str,
    traces_digest: str,
    bundles: list[dict],
    run_dir: Path,
    editor_cfg: dict,
    api_key: str,
    round_idx: int = 0,
) -> tuple[str, dict]:
    """Update native memory from bundles; render lossy canonical snapshot."""
    _ = traces_digest, round_idx
    memory_root = run_dir / "native_state" / "memp"
    memory_dir = memory_root / "memory"
    install_monkeypatches(
        embedding_model=(
            editor_cfg.get("embedding_id")
            or editor_cfg.get("embedding_model")
            or editor_cfg.get("id", "")
        ),
        embedding_base=(
            editor_cfg.get("embedding_api_base")
            or editor_cfg.get("embedding_base")
            or editor_cfg.get("api_base", "")
        ),
        embedding_key=(
            (k := editor_cfg.get("embedding_api_key") or "").upper() not in {"", "EMPTY"}
            and k
            or api_key
        ),
        reflector_model=(
            editor_cfg.get("reflector_id")
            or editor_cfg.get("reflector_model")
            or editor_cfg.get("id", "")
        ),
        reflector_base=(
            editor_cfg.get("reflector_api_base")
            or editor_cfg.get("reflector_base")
            or editor_cfg.get("api_base", "")
        ),
        reflector_key=(lambda k: k if k and k != "EMPTY" else api_key)(
            editor_cfg.get("reflector_api_key")
        ),
    )
    trajectories = [_bundle_to_trajectory(b) for b in bundles]
    base = [(skill_id, skill_body)]
    if memory_dir.is_dir() and _documents_json_path(memory_dir).is_file():
        mem, counts = _reflect_on_memory(memory_dir, trajectories)
        _ = mem
        telemetry = {"mode": "reflect_append", **counts}
    else:
        if memory_root.exists():
            shutil.rmtree(memory_root)
        memory_root.mkdir(parents=True)
        _, counts = _build_memory(memory_dir, trajectories, base_workflows=base)
        telemetry = {"mode": "cold_build", **counts}
    body, bridge_meta = render_memory_to_skill(memory_dir=memory_dir, skill_id=skill_id)
    if not bridge_meta.get("workflow_found"):
        bridge_meta["no_new_memory"] = True
    return body, {**telemetry, **bridge_meta}


def _reflect_on_memory(memory_dir: Path, trajectories: list[dict]) -> tuple[object, dict[str, int]]:
    """Append native Build + Update passes over new trajectories."""
    _ensure_memp_path()
    mem = _load_memory_from_dir(memory_dir, retrieve_num=3)
    counts = {"build_in": 0, "build_docs": 0, "reflect_in": 0, "reflect_adjusted": 0}

    new_documents = []
    for t in trajectories:
        item = {
            "source": t["task_id"],
            "query": t["instruction"][:_QUERY_CHARS],
            "trajectory": _trajectory_text(t)[:_TRAJ_CHARS],
            "reward": t.get("final_reward", 0.0),
            "task_id": t["task_id"],
        }
        counts["build_in"] += 1
        try:
            doc = mem.process_trajectory_item(item)
            if doc is not None:
                meta = getattr(doc, "metadata", None)
                if isinstance(meta, dict):
                    meta.setdefault("reward", item["reward"])
                    meta.setdefault("final_pass_rate", t.get("final_pass_rate", item["reward"]))
                    meta.setdefault("total_tests", t.get("total_tests", 0))
                new_documents.append(doc)
                counts["build_docs"] += 1
        except Exception as exc:
            logger.warning("memp iterative build failed for %s: %s", t["task_id"], exc)
    if new_documents:
        mem.documents.extend(new_documents)
        try:
            mem._save_documents()
        except Exception as exc:
            logger.warning("memp _save_documents after iterative build failed: %s", exc)

    for t in trajectories:
        counts["reflect_in"] += 1
        query = t["instruction"][:_QUERY_CHARS]
        traj_text = _trajectory_text(t)[:_TRAJ_CHARS]
        reward = t.get("final_reward", 0.0)
        workflow = ""
        try:
            results = mem.retrieve(str(query)) or []
        except Exception:
            results = []
        for r in results:
            doc = r[0] if isinstance(r, tuple) else r
            wf = (getattr(doc, "metadata", {}) or {}).get("workflow") or ""
            if wf:
                workflow = str(wf)
                break
        try:
            mem.process_trajectory_item_reflect(traj_text, reward, workflow)
            if not reward and workflow:
                counts["reflect_adjusted"] += 1
        except Exception as exc:
            logger.warning("memp iterative reflect failed: %s", exc)
    try:
        mem._save_documents()
    except Exception as exc:
        logger.warning("memp _save_documents failed: %s", exc)
    return mem, counts


__all__ = [
    "FRAMEWORK_TAG",
    "MEMP_HOME",
    "MEMP_REPO",
    "MempProducePlan",
    "edit_skill_from_traces",
    "produce",
    "install_monkeypatches",
    "_load_memory_from_dir",
    "render_memory_to_skill",
    "run_episode",
]
