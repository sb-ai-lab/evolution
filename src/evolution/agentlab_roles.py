"""Evolution roles used by the AgentLab package interface."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

from evolution.tree.roles.config import (
    ChatEditorSection,
    ModelGroupingSection,
    effective_grouping_v2,
)
from evolution.tree.roles.editor.chat_editor import ChatCredentials
from evolution.tree.roles.editor_contracts import EMPTY_HISTORY
from evolution.tree.roles.grouping.model_grouping_runtime import (
    ModelGroupingOutcome,
    new_grouping_client,
)
from evolution.tree.roles.grouping.model_grouping_v2 import run_model_grouping_v2
from evolution.tree.roles.grouping.portion_session import PortionOutcome
from evolution.tree.roles.grouping.reflect_grouping import (
    GroupingRecord,
    GroupingRegistry,
    build_registry,
)
from evolution.tree.roles.reflection_reader import (
    FrozenReflectionView,
    ReaderArgumentError,
    ReflectionReadSession,
)
from evolution.tree.roles.reflector.chat_reflector import ChatReflector
from evolution.tree.roles.reflector.reflect_brief import AdaptiveReflectBrief
from evolution.tree.roles.reflector.reflection_inputs import GROUPING_PAGE_SIZE, _page
from evolution.tree.roles.reflector_contracts import (
    REFLECT_LIMITS_V1,
    GroupError,
    ReflectRequest,
    admit_reflection_result,
)
from evolution.tree.roles.role_call import RoleCallRetry
from evolution.tree.roles.seals import seal_prompt


class _Failure(RuntimeError):
    """A closed error code with an optional diagnostic code, translated by the Lab."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


class _ModelChanged(_Failure):
    pass


class _Coverage:
    """Keep the IDs in admitted map groups. Canonical output numbers include fallback singletons."""

    def __init__(self) -> None:
        self.members: set[str] = set()

    def portion(self, outcome: PortionOutcome, plan_digest: str, input_digest: str) -> None:
        self.members.update(
            member["evidence_id"] for group in outcome.groups for member in group["members"]
        )

    def batch(self, *args: object) -> None:
        pass

    def record(self, value: Mapping[str, Any]) -> None:
        """Discard full prompts, conversations and model responses from Evolution's attempt log."""


def _wire(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(data: object) -> str:
    return hashlib.sha256(_wire(data).encode()).hexdigest()


def _registry(rows: list[dict]) -> GroupingRegistry:
    records = []
    for index, row in enumerate(rows):
        case = f"case{index:08d}"
        sources = {
            "case": {"case_id": case},
            "public": {"question": row["question"]},
            "feedback": {"judge": row["judgment"], "recorded_actions": row["actions"]},
            "observation": {
                "terminal": "completed",
                "critical": False,
                "primary_score": 0,
                "answer_text": [row["answer"]],
                "failed_criteria": ["benchmark_failure"],
                "verdicts": [{"criterion": "benchmark_failure", "passed": False}],
            },
        }
        records.append(GroupingRecord(row["id"], case, 1, True, True, sources))
    return build_registry(_digest(rows), records)


def _reader(registry: GroupingRegistry) -> ReflectionReadSession:
    def listing(args: Mapping[str, Any]) -> dict:
        start = args.get("offset", 0)
        total = len(registry.records)
        if type(start) is not int or not 0 <= start <= total:
            raise ReaderArgumentError("invalid_offset")
        end = min(total, start + GROUPING_PAGE_SIZE)
        entries = [
            {
                "index": i,
                "evidence_id": registry.records[i].evidence_id,
                "case_index": i,
                "repeat": 1,
                "available": True,
                "failing": True,
            }
            for i in range(start, end)
        ]
        return {"entries": entries, "total": total, "next_offset": end if end < total else None}

    def read(args: Mapping[str, Any]) -> dict:
        row = registry.records[args["index"]]
        sources = {
            **row.sources,
            "identity": {"case_id": row.case_id, "repeat": row.repeat},
            "metadata": row.sources["case"],
        }
        section = args["section"]
        if section not in sources:
            raise ReaderArgumentError("unknown_section")
        return _page(_wire(sources[section]), args.get("offset", 0))

    handlers = {"list_grouping_inputs": listing, "read_grouping_input": read}
    return FrozenReflectionView(handlers).open_operation("group-dialogues", set(handlers))


def _credentials(request: dict) -> ChatCredentials:
    address = urlsplit(request["endpoint"])
    proxy = None if proxy_bypass(address.netloc) else getproxies().get(address.scheme)
    return ChatCredentials(request["api_key"], proxy)


def _client(
    section: ModelGroupingSection | ChatEditorSection, credentials: ChatCredentials
) -> SimpleNamespace:
    client = new_grouping_client(section, credentials)

    async def create(**options: Any) -> Any:
        # Evolution's reduce stage and older installations omit Qwen's template option.
        if section.model.startswith("Qwen/"):
            options["extra_body"] = {
                **options.get("extra_body", {}),
                "chat_template_kwargs": {"enable_thinking": False},
            }
            options.setdefault("max_tokens", section.max_output_tokens)
        reply = await client.chat.completions.create(**options)
        if reply.choices and reply.model != section.model:
            raise _ModelChanged("model_changed")
        return reply

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=client.close
    )


def _groups(outcome: ModelGroupingOutcome, covered: set[str]) -> list[dict]:
    fallback = outcome.fallback
    if fallback and fallback.scope == "all":
        raise _Failure("full_fallback", fallback.reason)
    result = []
    for group in outcome.grouped.neighborhoods:
        description = group.description
        row: dict[str, Any] = {
            "members": list(group.member_ids),
            "title": description.label if description else "",
            "description": description.description if description else "",
        }
        if fallback and covered.isdisjoint(group.member_ids):
            row["fallback"] = asdict(fallback)
        result.append(row)
    return result


async def _group(request: dict) -> list[dict]:
    dialogues = request["dialogues"]
    if not dialogues:
        return []
    registry = _registry(dialogues)
    model, key = request["model"], request["api_key"]
    prompt = (
        files("evolution").joinpath("prompts/tree_grouping_model_v2.md").read_text(encoding="utf-8")
    )
    section = ModelGroupingSection(
        backend="openai_chat",
        model=model,
        base_url=request["endpoint"],
        api_key_env=None,
        timeout_seconds=435,
        concurrency=1,
        max_input_chars=140_000,
        max_output_tokens=7000,
        retry=RoleCallRetry(2, 430, 1, 2, 5),
        glimpse_chars=1600,
    )
    coverage = _Coverage()
    with _reader(registry) as reader:
        outcome = await run_model_grouping_v2(
            registry,
            section=section,
            limits=effective_grouping_v2(section),
            prompt=prompt,
            metric_primary="benchmark_failure",
            policy_identity=_digest([model, prompt]),
            credentials=_credentials(request),
            log=coverage,
            session_seconds=1600,
            reader=reader,
            provenance=None,
            checkpoints=coverage,
            adopted=None,
            cancelled=lambda: False,
            forbidden_secret=lambda text: bool(key and key in text),
            oracle_needle=lambda text: False,
            record_budget=60_000,
            client_factory=_client,
        )
    return _groups(outcome, coverage.members)


_FOCUS = (
    "Investigate only the selected AgentLab problem and its confirming cases. Consider whether different "
    "mechanisms underlie similar symptoms. Cite only the selected evidence and supplied prompt files. "
    "State untested assumptions or decline when evidence is insufficient. No files may be edited. "
    "Write the hypothesis and suggested fix in Russian for the AgentLab user. Use Russian for risks and "
    "explanations as well. Keep exact evidence identifiers, paths and quotations in their original form."
)


def _evidence(cases: list[dict]) -> tuple[dict, dict, list[dict]]:
    sources, ids, entries = {}, {}, []
    for row in cases:
        unit, case = row["unit_id"], row["dialogue_id"]
        if unit in ids or not unit or not case:
            raise _Failure("evidence")
        ids[unit] = case
        sources[unit] = {
            "identity": {"case_id": case, "evidence_id": unit},
            "public": {"question": row.get("question"), "history": row.get("history")},
            "observation": {"answer_text": row.get("raw_reply") or row.get("reply")},
            "feedback": {
                field: row[field]
                for field in (
                    "observed",
                    "conditions",
                    "reason",
                    "symptoms",
                    "cited",
                    "verdict",
                    "caveats",
                    "contested",
                    "foreign",
                    "requirement_key",
                )
                if field in row
            },
        }
        entries.append({"case_id": case, "evidence_id": unit, "state": "failing"})
    return sources, ids, entries


def _list_evidence(entries: list[dict], args: Mapping[str, Any]) -> dict:
    selected = [
        entry for entry in entries if not args.get("case_id") or entry["case_id"] == args["case_id"]
    ]
    start = args.get("offset", 0)
    if type(start) is not int or not 0 <= start <= len(selected):
        raise ReaderArgumentError("invalid_offset")
    end = min(start + GROUPING_PAGE_SIZE, len(selected))
    return {
        "entries": selected[start:end],
        "total": len(selected),
        "next_offset": end if end < len(selected) else None,
    }


def _read_evidence(sources: dict, args: Mapping[str, Any]) -> dict:
    source = sources.get(args.get("evidence_id"))
    if source is None:
        raise ReaderArgumentError("unknown_evidence")
    section = args.get("section")
    if section and section not in source:
        raise ReaderArgumentError("unknown_section")
    return _page(_wire(source[section] if section else source), args.get("offset", 0))


async def _reflect(request: dict, workspace: Path | None = None) -> dict:
    sources, ids, entries = _evidence(request["cases"])
    paths = tuple(request["paths"])
    response = {"model": request["model"], "hypotheses": [], "groups": []}
    insufficient = response | {
        "status": "insufficient-evidence",
        "note": "Для гипотез недостаточно свидетельств.",
    }
    if not ids or not paths:
        return insufficient
    examples: dict[str, list[str]] = {}
    for unit, case in ids.items():
        examples.setdefault(case, []).append(unit)
    payload = {
        "portion": [
            {"case_id": case, "exemplars": units, "same_case_success": None}
            for case, units in examples.items()
        ],
        "operator_context": _FOCUS,
        "selected_problem": request["problem"],
        "requirements": request["requirements"],
        "evidence_catalog": _list_evidence(entries, {}),
        "editable_paths": list(paths),
        "target_catalog": request["target_catalog"],
        "limits": {"sentence_chars": 512},
    }
    brief = AdaptiveReflectBrief(
        _digest(request["problem"]),
        _FOCUS,
        EMPTY_HISTORY,
        paths,
        REFLECT_LIMITS_V1,
        payload,
        ids,
        frozenset(ids),
        None,
    )
    handlers = {
        "list_evidence": lambda args: _list_evidence(entries, args),
        "read_evidence": lambda args: _read_evidence(sources, args),
    }
    section = ChatEditorSection(
        request["model"],
        request["endpoint"],
        None,
        reasoning_effort="low",
        max_turns=18,
        retry=RoleCallRetry(2, 180, 1, 2, 5),
        max_output_tokens=12000,
        context_compact_chars=180000,
        context_message_max_chars=12000,
    )
    root = Path(request["root"])
    credentials = _credentials(request)
    secrets = tuple(s for s in (credentials.api_key, credentials.proxy_url) if s)
    prompt = files("evolution").joinpath("prompts/tree_reflector.md").read_text(encoding="utf-8")
    role = ChatReflector(
        section,
        credentials,
        seal_prompt(None, packaged=prompt),
        root / "logs",
        secrets,
        client_factory=_client,
        role_resilience=True,
        bounds_repair=True,
    )
    with FrozenReflectionView(handlers).open_operation("card-reflection", set(handlers)) as reader:
        session = ReflectRequest(
            "selected-card",
            1,
            workspace if workspace is not None else root / "parent",
            brief,
            None,
            None,
            paths,
            300,
            reader=reader,
        )
        outcome = await role.reflect(session)
    if outcome.status == "declined":
        return insufficient
    if outcome.status != "reflected":
        if outcome.note == "exception:_ModelChanged":
            raise _ModelChanged("model_changed")
        raise _Failure("reflection_failed", outcome.code)
    try:
        groups = admit_reflection_result(
            outcome,
            catalog_ids=ids,
            case_ids=set(ids.values()),
            editable_paths=paths,
            probe_seqs=None,
            needles=(),
            runtime_secrets=secrets,
            observation_ids=ids,
            failing_evidence_ids=frozenset(ids),
        )
    except GroupError as exc:
        raise _Failure("invalid_hypothesis", exc.code) from exc
    return {
        "model": section.model,
        "status": "hypotheses",
        "hypotheses": [group.root_cause for group in groups],
        "groups": [group.to_payload() for group in groups],
        "note": "",
    }
