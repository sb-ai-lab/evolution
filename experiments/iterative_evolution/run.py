#!/usr/bin/env python3
"""CLI for iterative skill-evolution loop."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WS_ROOT = REPO_ROOT.parent
if str(REPO_ROOT / "experiments") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "experiments"))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from _shared.sbbench_cell_resolver import resolve_cells  # noqa: E402

from iterative_evolution.loop import run_cell  # noqa: E402


def _default_corpus() -> Path:
    env = os.environ.get("AFTER_DATA_ROOT", "")
    if env:
        return Path(env)
    return WS_ROOT / "datasets" / "AFTER"


def _served_id_for(model: str) -> str:
    if model.startswith("openai/openai/"):
        return model.removeprefix("openai/")
    return model


def _extra_body_for(model: str, reasoning_effort: str) -> dict | None:
    if "gpt-oss" not in (model or "").lower():
        return None
    effort = (reasoning_effort or "").strip()
    return {"reasoning_effort": effort} if effort else None


def _load_solver_cfg(args: argparse.Namespace) -> tuple[dict, dict, dict, dict, str, str, str, str]:
    api_key = (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or "EMPTY"
    )
    editor_api_key = (
        args.editor_api_key
        or os.environ.get("EVO_EDITOR_API_KEY")
        or os.environ.get("LOCAL_VLLM_API_KEY")
        or "EMPTY"
    )
    served = args.served_id or _served_id_for(args.model)
    editor_model = args.editor_model or args.model
    editor_served = args.editor_served_id or _served_id_for(editor_model)
    editor_base = args.editor_api_base or args.api_base
    solver = {
        "id": args.model,
        "litellm_id": args.model,
        "served_id_hint": served,
        "api_base": args.api_base,
        "max_output_tokens": int(args.max_tokens),
        "temperature": float(args.temperature),
    }
    solver_extra = _extra_body_for(f"{args.model} {served}", args.solver_reasoning_effort)
    if solver_extra:
        solver["extra_body"] = solver_extra
    embedding = {
        "id": args.embedding_model,
        "api_base": args.embedding_api_base,
        "embedding_id": args.embedding_model,
        "embedding_api_base": args.embedding_api_base,
    }
    reflector = {
        "id": args.reflector_model or editor_model,
        "litellm_id": args.reflector_model or editor_model,
        "served_id_hint": args.reflector_model or editor_served,
        "api_base": args.reflector_api_base or editor_base,
        "max_output_tokens": int(args.editor_max_tokens or args.max_tokens),
        "temperature": float(
            args.editor_temperature if args.editor_temperature is not None else args.temperature
        ),
    }
    reflector_extra = _extra_body_for(
        f"{reflector['id']} {reflector['served_id_hint']}", args.editor_reasoning_effort
    )
    if reflector_extra:
        reflector["extra_body"] = reflector_extra
    editor = {
        "id": editor_model,
        "litellm_id": editor_model,
        "served_id_hint": editor_served,
        "api_base": editor_base,
        "max_output_tokens": int(args.editor_max_tokens or args.max_tokens),
        "temperature": float(
            args.editor_temperature if args.editor_temperature is not None else args.temperature
        ),
        "embedding_id": args.embedding_model,
        "embedding_api_base": args.embedding_api_base,
        "embedding_api_key": args.embedding_api_key,
        "reflector_id": reflector["served_id_hint"],
        "reflector_api_base": reflector["api_base"],
        "reflector_api_key": args.reflector_api_key,
        "evoskill_concurrency": int(args.concurrency),
    }
    editor_extra = _extra_body_for(f"{editor_model} {editor_served}", args.editor_reasoning_effort)
    if editor_extra:
        editor["extra_body"] = editor_extra
    return (
        solver,
        editor,
        embedding,
        reflector,
        api_key,
        editor_api_key,
        args.embedding_api_key,
        args.reflector_api_key,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Iterative skill-evolution loop")
    parser.add_argument(
        "--framework",
        required=True,
        help="hermes|codex|claude|evoskill|memento|memp|evolution",
    )
    parser.add_argument("--skill", required=True, help="anchor skill id, e.g. pipelines")
    parser.add_argument("--cell", required=True, help="role.skill cell id, e.g. de.pipelines")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument(
        "--model", default=os.environ.get("EVO_SOLVER_MODEL", "deepseek/deepseek-v4-flash")
    )
    parser.add_argument("--served-id", default=os.environ.get("EVO_SOLVER_SERVED_ID", ""))
    parser.add_argument(
        "--api-base", default=os.environ.get("EVO_SOLVER_API_BASE", "https://openrouter.ai/api/v1")
    )
    parser.add_argument("--max-tokens", default="8000")
    parser.add_argument("--temperature", default="0.2")
    parser.add_argument(
        "--solver-reasoning-effort",
        default=os.environ.get(
            "EVO_SOLVER_REASONING_EFFORT", os.environ.get("EVO_LLM_REASONING_EFFORT", "medium")
        ),
    )
    parser.add_argument("--editor-model", default=os.environ.get("EVO_EDITOR_MODEL", ""))
    parser.add_argument("--editor-served-id", default=os.environ.get("EVO_EDITOR_SERVED_ID", ""))
    parser.add_argument("--editor-api-base", default=os.environ.get("EVO_EDITOR_API_BASE", ""))
    parser.add_argument("--editor-api-key", default=os.environ.get("EVO_EDITOR_API_KEY", ""))
    parser.add_argument("--editor-max-tokens", default=os.environ.get("EVO_EDITOR_MAX_TOKENS", ""))
    parser.add_argument("--editor-temperature", type=float, default=None)
    parser.add_argument(
        "--editor-reasoning-effort",
        default=os.environ.get(
            "EVO_EDITOR_REASONING_EFFORT", os.environ.get("EVO_LLM_REASONING_EFFORT", "medium")
        ),
    )
    parser.add_argument(
        "--embedding-model",
        default=os.environ.get("EVO_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
    )
    parser.add_argument(
        "--embedding-api-base",
        default=os.environ.get("EVO_EMBEDDING_API_BASE", "http://localhost:10225/v1"),
    )
    parser.add_argument(
        "--embedding-api-key",
        default=os.environ.get("EVO_EMBEDDING_API_KEY", "EMPTY"),
    )
    parser.add_argument("--reflector-model", default=os.environ.get("EVO_REFLECTOR_MODEL", ""))
    parser.add_argument(
        "--reflector-api-base", default=os.environ.get("EVO_REFLECTOR_API_BASE", "")
    )
    parser.add_argument(
        "--reflector-api-key",
        default=os.environ.get("EVO_REFLECTOR_API_KEY", "EMPTY"),
    )
    parser.add_argument(
        "--concurrency", type=int, default=int(os.environ.get("EVO_CONCURRENCY", "16"))
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    corpus = Path(args.corpus or _default_corpus()).resolve()
    role, skill = args.cell.split(".", 1)
    if skill != args.skill:
        print(
            f"warning: --skill {args.skill!r} differs from cell anchor {skill!r}", file=sys.stderr
        )

    resolved = resolve_cells(corpus_root=corpus, selected_cells=(args.cell,))
    cell = resolved["cells"][0]
    train_ids = cell["streams"]["train_a"] + cell["streams"]["train_b"]
    test_ids = cell["streams"]["test"]

    run_dir = Path(
        args.run_dir
        or (
            REPO_ROOT
            / "experiments"
            / "iterative_evolution"
            / "runs"
            / f"{args.framework}__{args.cell}__{args.skill}"
        )
    ).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    (
        solver_cfg,
        editor_cfg,
        embedding_cfg,
        reflector_cfg,
        api_key,
        editor_api_key,
        emb_key,
        refl_key,
    ) = _load_solver_cfg(args)

    manifest_path = run_dir / "cell_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "cell_id": args.cell,
                "skill_id": args.skill,
                "framework": args.framework,
                "corpus_root": str(corpus),
                "train_ids": train_ids,
                "test_ids": test_ids,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    summary = run_cell(
        framework=args.framework,
        skill_id=args.skill,
        role=role,
        train_ids=train_ids,
        test_ids=test_ids,
        n_rounds=args.rounds,
        solver_cfg=solver_cfg,
        editor_cfg=editor_cfg,
        api_key=api_key,
        editor_api_key=editor_api_key,
        run_dir=run_dir,
        corpus_root=corpus,
        embedding_cfg=embedding_cfg,
        reflector_cfg=reflector_cfg,
        emb_key=emb_key,
        refl_key=refl_key,
        dry_run=args.dry_run,
    )
    print(
        json.dumps({"run_dir": str(run_dir), "best_version": summary.get("best_version")}, indent=2)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
