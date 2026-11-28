"""Domain-independent reports from core's public projections, never raw traces."""
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from evolution.tree import inspect_journal, project_result
from evolution.tree import CampaignProjection
from evolution_lab.owner.core_raw import LEGACY_FAMILIES, CampaignRead, CoreRawUnavailable, campaign_read
from evolution.tree.execution.journal_projections import Projection

from evolution_lab._outcomes import MAX_EVALUATION_RESULT_BYTES
from evolution_lab._independent_text import (
    attempt_progress_text, preflight_parts, quantity, reference_text, repeat_variance_text,
    round_progress_text, scope_statement, stages_text, with_checks,
)
from evolution_lab._notice_text import CLEAN_ROUNDS_RU, FOCUS_SIGNAL_RU, FOCUS_UNAVAILABLE_RU, BASELINE_PROVENANCE_UNAVAILABLE_RU, all_rounds_clean
from evolution_lab._suspension import last_suspension, suspension_hint
from evolution_lab.provenance import WheelPin


@dataclass(frozen=True)
class CostSummary:
    currency: str
    limit_units: int
    upper_units: int
    requests: int
    unknown_requests: int
    refusals: int
    overrun_requests: int = 0
    bound_observations: int = 0

    def __post_init__(self) -> None:
        if self.currency not in {"USD", "RUB"}:
            raise ValueError("explicit supported accounting currency required")
        for key, value in asdict(self).items():
            if key != "currency" and (type(value) is not int or value < 0):
                raise ValueError("invalid cost summary")
        if self.overrun_requests > self.requests:
            raise ValueError("overruns exceed total requests")
        if self.bound_observations > self.requests:
            raise ValueError("bound observations exceed total requests")
        if self.unknown_requests > self.requests:
            raise ValueError("unknown requests exceed total requests")

    @classmethod
    def from_budget(cls, summary: Mapping[str, Any], *, currency: str) -> "CostSummary":
        return cls(currency, summary["limit_units"], summary["reserved_upper_units"],
                   summary["requests"], summary["unknown_requests"], summary["refusals"], summary.get("overrun_requests", 0),
                   summary.get("bound_observations", 0))


def _notice_md(notice: dict) -> str:
    if notice.get("code") == "baseline_provenance_unknown":
        from evolution_lab._notice_text import BASELINE_PROVENANCE_RU
        return BASELINE_PROVENANCE_RU + f" (round={notice['round']}, measurement_id={notice['measurement_id']}, samples={notice['samples']})"
    if notice.get("code") == "scorer_without_focus_signal":
        return FOCUS_SIGNAL_RU + f" (admitted={notice['admitted']}, not_completed={notice['not_completed']})"
    counts = ", ".join(f"{_cell(k)} — {v}" for k, v in (notice.get("by_reason") or {}).items()) or "—"
    text = f"**Ни один кандидат не замерен** (уведомление Lab, итог остаётся словом ядра): {counts}."
    if notice.get("reflection_errors"):
        text += " Ошибки рефлексии: " + ", ".join(
            f"{_cell(k)} — {v}" for k, v in notice["reflection_errors"].items()) + "."
    if all_rounds_clean(notice):
        text += " " + CLEAN_ROUNDS_RU
    return text


def _reflections_unavailable_md(data: Mapping[str, Any]) -> str | None:
    """The failed read of the round reflections, said next to the empty value (ADR-0019 p.3)."""
    if data.get("lab_rounds_state") != "unavailable":
        return None
    return f"Подробности рефлексии недоступны ({_cell(data.get('lab_rounds_reason') or 'unreadable')})."


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    text = str(value).replace("\n", " ").replace("\r", " ")
    text = re.sub(r"([\\`*\[\]<>|&])", r"\\\1", text)
    return re.sub(r"(?<!\w)_(?=\S)|(?<=\S)_(?!\w)", r"\\_", text)


def _fields(value: Any) -> str:
    if isinstance(value, Mapping):
        return ", ".join(f"{_cell(key)}={_fields(item)}" for key, item in value.items()) or "—"
    if isinstance(value, list):
        return "; ".join(_fields(item) for item in value) or "—"
    return _cell(value)


def _code(value: Any) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ").replace("|", "\\|")
    longest = max((len(match) for match in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _number(value: Any) -> str:
    return "—" if value is None else f"{value:.6g}"


def _baseline_import_lines(core: Mapping[str, Any]) -> list[str]:
    block = core.get("baseline_import")
    if block is None:
        return []
    from evolution_lab.owner.campaign_views import baseline_import_fields

    facts = baseline_import_fields(block)
    source = _code(block["from_run"])
    lines = [
        f"Baseline из оценки {source}. Импортировано пар {_cell(facts['imported'])}. "
        f"Пар к замеру {_cell(facts['to_measure'])}.",
    ]
    if block["abandoned"]:
        lines.append("Core отменил импорт. Baseline замеряется полностью.")
    counts = ", ".join(_code(f"{name}={count}") for name, count in facts["not_imported"].items())
    lines.append(f"Строки источника вне импорта — {counts}.")
    if facts["fields"]:
        lines.append("Отличия условий — " + ", ".join(map(_code, facts["fields"])) + ".")
    for name, label in (
        ("authorization_id", "Решение о совместимости"),
        ("held_out_authorization_id", "Решение по held-out кейсам"),
    ):
        if facts[name] is not None:
            lines.append(f"{label} {_code(facts[name])}.")
    for caveat in core.get("caveats", ()):
        if caveat["kind"] == "imported_provenance_unknown":
            lines.append(f"{_code(caveat['kind'])} — provenance неизвестна у {_cell(caveat['unknown'])} импортированных строк.")
    return [*lines, ""]


def _gate_lines(core: Mapping[str, Any]) -> list[str]:
    """Family 14: the gate mode, core's caveats and its corpus split, as core gave them."""
    from evolution_lab.owner.campaign_views import gate_mode_of
    try:
        mode = gate_mode_of(core)
        caveats = list(core.get("caveats") or ())
    except Exception:  # caveats unreadable (ADR-0019 p.3): the gate is unknown, never read as statistical
        return ["Гейт уверенности: неизвестен — caveat-ы не читаются (семья 14); результат не "
                "является свидетельством качества.", ""]
    lines = [f"Гейт уверенности: {_cell(mode)} (семья 14)."]
    if mode == "observe":
        kinds = [item["kind"] for item in caveats if item.get("round") is None]
        lines.append("Гейт не применялся (observe): результат не является свидетельством качества ("
                     + ", ".join(_code(kind) for kind in kinds) + ").")
    scoped = [item for item in caveats if item.get("round") is not None]
    if scoped:
        lines.append("Caveat-ы попыток: " + "; ".join(
            f"раунд {_cell(item['round'])}, {_cell(item.get('attempt_id'))}: {_code(item['kind'])}"
            for item in scoped) + ".")
    split = core.get("corpus_split")
    if isinstance(split, Mapping):
        lines.append(f"Корпус: разметка {_cell(split['mode'])}; train {_cell(split['train_cases'])}, "
                     f"test {_cell(split['test_cases'])}.")
    return lines + [""]


def _gate_cells(attempt: Mapping[str, Any]) -> tuple[str, str]:
    from evolution_lab.owner.campaign_views import confidence_of
    folded = confidence_of(attempt.get("confidence"))
    latest = folded["latest"] if folded else None
    if latest is None:
        return "—", "—"
    focus = latest["focus"]
    return (f"{latest['decision']}/{latest['reason']}",
            "—" if focus is None else f"{_number(focus['point'])} [{_number(focus['lower'])}, {_number(focus['upper'])}]")


def _family_14_lines(core: Mapping[str, Any]) -> list[str]:
    """Family 14 (spec 2026-10-13 §7.2): settings, roles and the connection check, as core gave them."""
    settings = core.get("settings") or {}
    refresh = settings.get("baseline_refresh")
    refresh_text = (f": раундов между обновлениями {_cell(refresh.get('every_rounds'))}, выполнено "
                    f"{len(refresh.get('performed') or ())}" if isinstance(refresh, Mapping) else f": {_cell(refresh)}")
    return [f"Настройки: стабильность {_cell(settings.get('stability'))}, остановка раунда "
            f"{_cell(settings.get('round_stop'))}, обновление baseline{refresh_text}.", *_roles_lines(core), ""]


def _roles_lines(core: Mapping[str, Any]) -> list[str]:
    """Roles and the connection check, as core gave them (family 14 and 15 alike)."""
    roles, connection = core.get("roles") or {}, core.get("connection") or {}
    lines = ["Роли: " + ", ".join(f"{label} {_cell((roles.get(key) or {}).get('kind'))}" for key, label in
                                  (("editor", "Editor"), ("reflector", "Reflector"), ("grouper", "Grouper"))) + "."]
    detail = connection.get("detail")
    code = (detail.get("code") or detail.get("cause_code")) if isinstance(detail, Mapping) else None
    lines.append(f"Проверка подключения: {_cell(connection.get('outcome') or connection.get('state'))} (попыток "
                 f"{_cell(connection.get('attempts'))}" + (f", код {_code(code)}" if code else "") + ").")
    return lines


def _ru(q: Any) -> str:
    return quantity(q, "ru")


def _preflight_md(explanation: Mapping[str, Any]) -> str:
    """Core's preflight status and basis, then its ceilings per calculated scope with Core's own ``passes``."""
    parts = preflight_parts(explanation, "ru", _cell)
    return (f"{_cell(explanation.get('status'))} ({_cell(explanation.get('basis'))})"
            + (": " + "; ".join(parts) if parts else ""))


def _cost_md(cost: Mapping[str, Any]) -> str:
    """Core's conditional cost of one attempt: measurement slots of a complete no-retry scenario, never money."""
    return (f"- Условная стоимость в слотах замеров (не деньги): кандидат {_cell(cost['candidate_total_slots'])} "
            f"(гейт {_cell(cost['candidate_gate_slots'])}, финал {_cell(cost['candidate_final_slots'])}), "
            f"baseline {_cell(cost['baseline_slots'])}, свежий замер чемпиона: гейт {_cell(cost['parent_slots'])}, "
            f"финал {_cell(cost['champion_slots'])} (основа: {_cell(cost['champion_basis'])}); простейший путь к прохождению "
            f"{_cell(cost['simplest_passing_path_slots'])}.")


def _family_15_lines(independent: Mapping[str, Any] | None, reason: str | None = None, *,
                     checks: Mapping[str, Any] | None = None, checks_state: str = "present") -> list[str]:
    return with_checks(_independent_markdown_lines(independent, reason), checks, checks_state, _cell)


def _independent_markdown_lines(independent: Mapping[str, Any] | None, reason: str | None = None) -> list[str]:
    """Family 15 (spec 2026-10-19 D7): the ``lab_independent`` block of the view (``independent_view``) in Russian.

    One settings line; unauthenticated evidence said so (never "no attempts"); the round's attainability; per attempt
    Core's outcome and reason, its comparison against the round's champion with the measurement named, one statement
    per scope by its ``display`` (``_independent_text``), or, without a comparison, its preflight ceilings; the
    conditional cost in measurement slots. Lab decides nothing: ``→`` copies Core's comparison outcome."""
    if not isinstance(independent, Mapping):
        said = f" ({_code(reason)})" if reason else ""   # ``lab_independent_reason``, the explicit state's code
        return [f"Независимая проверка (семья 15): блок ядра не читается этой версией Lab{said}; "
                "сравнения не показаны.", ""]
    settings = independent["settings"]
    noise = settings["execution_noise"]
    lines = ["Гейт: независимая проверка ядра (семья 15); решение принимает ядро, Lab его не пересчитывает.",
             f"Настройки: R {_cell(settings['repeats'])}, режим {_cell(settings['mode'])}, шум исполнения "
             f"{_cell(noise['kind'])} (допуск {_ru(noise['allowance'])}, {_cell(noise['source_kind'])}), проверка "
              f"{_cell(settings['verification'])}, fresh_parent {'да' if settings['fresh_parent'] is True else 'нет'}."]
    if settings.get("bound_fallback") is not None:
        lines.append(f"Откат границы (bound_fallback): {_cell(settings['bound_fallback'])}.")
    if independent["evidence"] != "authenticated_publications":
        lines.append(f"Сравнения недоступны (свидетельство ядра {_code(independent['evidence'])}): публикации "
                     "попыток не подтверждены; это не значит, что попыток не было.")
    for row in independent.get("round_progress", ()):
        lines.extend(round_progress_text(row, "ru", _cell))
    attainability = independent["attainability"]
    for item in attainability["preflight"]:
        lines.append(f"Раунд {_cell(item['round'])} — предварительная проверка достижимости: "
                     f"{_preflight_md(item['explanation'])}.")
    guidance = attainability["local_guidance"]
    if guidance is not None:
        lines.append(f"Ориентир ядра: не меньше {_cell(guidance['minimum_units'])} единиц "
                     f"({_cell(guidance['status'])}, {_cell(guidance['interpretation'])}).")
    for attempt in independent["attempts"]:
        head = (f"Попытка {_cell(attempt['attempt_id'])} (раунд {_cell(attempt['round'])}): исход "
                f"{_cell(attempt['outcome'])}, причина {_cell(attempt['reason'])}")
        comparisons = [("сравнение со шлюзом", attempt["gate_comparison"]),
                       ("глобальное сравнение", attempt["global_comparison"])]
        present = [(label, comparison) for label, comparison in comparisons if comparison is not None]
        lines.append("")
        if not present:
            lines.append(head + "; сравнения нет.")
            if attempt["preflight"] is not None:
                lines.append(f"- Предварительная проверка: {_preflight_md(attempt['preflight'])}.")
        else:
            lines.append(head + ".")
        if "progress" in attempt:
            lines.extend(f"- {line}" for line in attempt_progress_text(attempt["progress"], "ru", _cell))
        if "stages" in attempt:
            lines.append(f"- {stages_text(attempt['stages'])}")
        for layer, label in (("gate", "шлюз"), ("global", "глобальное")):
            binding = attempt.get(f"{layer}_binding")
            if binding is not None:
                rules = ", ".join(f"{_cell(s['name'])}={_cell(s['rule'])}" for s in binding["scopes"])
                lines.append(f"- Правила binding ({label}, core_raw): {rules}.")
        for label, comparison in present:
            reference_id = comparison["reference_measurement_id"]
            short_id = _cell(reference_id[:12]) if reference_id else "—"
            lines.append(f"- {label} {_cell(comparison['outcome'])} ({_cell(comparison['reason'])}), "
                         f"решающий слой {_cell(comparison['deciding_layer'])} ({_cell(comparison['layer'])}), "
                          f"{reference_text(comparison, 'ru')}, эталон {short_id}.")
            if comparison.get("calibration") is not None:
                calibration = comparison["calibration"]
                lines.append(f"- Калибровка: {_cell(calibration['source'])}, {_cell(calibration['method'])}, "
                             f"уровень {_ru(calibration['level'])}, допуск {_ru(calibration['allowance'])}.")
            if comparison.get("caveats"):
                lines.append("- Caveat-ы сравнения: " + ", ".join(map(_cell, comparison["caveats"])) + ".")
            variance = repeat_variance_text(comparison.get("noise") or {}, "ru", _cell)
            if variance is not None:
                lines.append(f"- {variance}.")
            for scope in comparison["scopes"]:
                text = scope_statement(scope, "ru", _cell)
                layer = "global" if comparison["layer"] == "global" else "gate"
                if attempt.get(f"{layer}_binding") is not None:
                    text = f"правило результата {_cell(scope['rule'])}; {text}"
                if scope["name"] == comparison["deciding_layer"]:
                    text += f" → {_cell(comparison['outcome'])}"
                lines.append(f"- {_cell(scope['name'])}: {text}")
        if attempt["eligible_final_score"] is not None:
            lines.append(f"- Итоговый балл (eligible_final_score): {_ru(attempt['eligible_final_score'])}")
        if attempt["conditional_cost"] is not None:
            lines.append(_cost_md(attempt["conditional_cost"]))
    return lines + [""]


def _final_lines(rounds: Sequence[Mapping[str, Any]]) -> list[str]:
    """Family 14's final (spec §7.2): members, point values, exact contrasts, the choice, unmeasured
    pairs; rows of the listing are not shown."""
    from evolution_lab.owner.campaign_views import final_of
    lines = []
    for row in rounds:
        for entry in final_of(row.get("final")) or ():
            finalists = ", ".join(f"{_cell(m['attempt_id'])} ({_number(m['point'])})"
                                  for m in entry["members"] if m["role"] == "finalist") or "—"
            exact = [c["attempt_id"] for c in entry["contrasts"] if c["exact"] is True]
            selection = entry["selection"]
            if selection is None:
                chosen = ""
            elif selection["attempt_id"] is None:
                chosen = f"; выбранного нет ({_code(selection['reason'])})"
            else:
                chosen = f"; выбран {_cell(selection['attempt_id'])} ({_code(selection['reason'])})"
            lines.append(f"Раунд {_cell(row['round'])} — финал (запись {entry['ordinal']}, {_cell(entry['mode'])}, "
                         f"global {_cell(entry['global'])}, стабильность {_cell(entry['stability'])}): финалисты "
                         f"{finalists}" + (f"; точные контрасты: {', '.join(map(_cell, exact))}" if exact else "")
                         + chosen + (f"; недомерено пар: {entry['listed']}" if entry["listed"] else "") + ".")
    return lines


def _measurement(projection: Projection, expected: int | None,
                 family: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = projection.to_payload()
    if expected is not None and (type(expected) is not int or expected < 0):
        raise ValueError("invalid expected sample count")
    scored = [row for row in source["samples"] if row["scoring_state"] == "finished"]
    if expected is not None and expected < len(source["samples"]):
        raise ValueError("expected coverage contradicts recorded samples")
    excluded = None if family is None else family["excluded_pairs"]
    if expected is not None and excluded is not None:
        # Family 6: core removed exhausted baseline pairs from both sides; they
        # are not owed, so coverage is measured against the rest of the schedule.
        expected -= excluded["count"]
        if expected < 0:
            raise ValueError("excluded pairs exceed the schedule")
    from evolution_lab.owner.measurements import metric_means  # local import: no cycle via owner

    state = "unknown" if expected is None else (
        "complete" if source["complete"] and expected == len(scored) else "incomplete"
    )
    row = {
        "run_id": source["run_id"], "candidate_digest": source["candidate_digest"],
        "coverage": {"expected": expected, "scored": len(scored), "state": state},
        "tries": None if family is None else family["tries"],
        "excluded_pairs": excluded,
        "metrics": metric_means(scored),
        "samples": source["samples"], "journal_integrity": source["journal_integrity"],
        "references": source["references"],
    }
    if family is not None and "call_retries" in family:
        # Family 7: core's per-measurement call evidence, as projected.
        row["call_retries"] = family["call_retries"]
        row["call_failures_by_code"] = family["call_failures_by_code"]
    return row


@dataclass(frozen=True)
class Report:
    _encoded: str

    @property
    def data(self) -> dict[str, Any]:
        return json.loads(self._encoded)

    def _owner_continuation(self) -> str:
        target = self.data.get("owner_resume_target")
        if not target:
            return ("Операция Owner продолжается `lab resume` (Python: `Owner.resume`) "
                    "на готовой цели того же Bundle; если операция требует очистки — "
                    "сначала `lab cleanup`; после ручного `lab release-operation` продолжение недоступно.")
        used = ", ".join(_code(ref["id"]) for ref in target["excluded"])
        ready = target.get("ready_target")
        if ready:
            return ("Операция Owner продолжается `lab resume` (Python: `Owner.resume`) на готовой цели "
                    f"{_code(ready['id'])} того же Bundle, которую кампания не использовала (уже использованы: {used}): "
                    "последняя приостановка — `target_unstable`; если операция требует очистки — сначала "
                    "`lab cleanup`; после ручного `lab release-operation` продолжение недоступно.")
        return ("Операция Owner продолжается `lab resume` (Python: `Owner.resume`) на свежей цели того же "
                f"Bundle — не на цели, которую кампания уже использовала ({used}): последняя "
                "приостановка — `target_unstable`; если операция требует очистки — сначала "
                "`lab cleanup`; после ручного `lab release-operation` продолжение недоступно.")

    def markdown(self) -> str:
        data = self.data
        core = data["core"]
        campaign = data["operation"] == "campaign"
        lines = ["# " + ("Кампания" if campaign else "Самостоятельная оценка"), "",
                 "Run: " + _cell(core["run_id"]), "", "Сравнение: " + _cell(data["comparison_scope"]), ""]
        if not campaign:
            if data.get("lab_notices_state") == "unavailable":
                lines.extend([FOCUS_UNAVAILABLE_RU, ""])
            for notice in data.get("lab_notices") or ():
                lines.extend([_notice_md(notice), ""])
        if campaign:
            from evolution_lab._reflector_probe import probe_lines
            lines.extend(probe_lines(data))
            lines.extend(["Целостность журнала кампании: " + _cell(data.get("campaign_journal_integrity", "не проверена")),
                          "Исход ниже относится к прочитанному core префиксу журнала.", ""])
            outcome = core["campaign_outcome"]
            lines.extend(["Исход core: " + (_cell(outcome["outcome"]) if outcome else "терминального исхода core нет"),
                          "Причина: " + (_cell(outcome["reason"]) if outcome else "—"), ""])
            lines.extend(_baseline_import_lines(core))
            if data.get("lab_notices_state") == "unavailable":
                lines.extend([BASELINE_PROVENANCE_UNAVAILABLE_RU, ""])
            for notice in self.data.get("lab_notices") or ():
                lines.extend([_notice_md(notice), ""])
            empty = [row for row in core["rounds"] if not row["attempts"]]
            unread = _reflections_unavailable_md(data)
            if unread and not empty:  # with rounds without attempts it stands under their table, by the dashes
                lines.extend([unread, ""])
            if outcome and outcome["reason"] == "condition_changed":
                lines.extend(["Кампания закрыта авторизацией оператора: изменилось сравнимое условие "
                              "(`condition_changed`). Начните связанное продолжение: `lab campaign DIR --from OPERATION`.", ""])
            family_15 = core.get("family_version") == 15
            if family_15:
                from evolution_lab.owner.core_raw import family15_probe_lines
                try:
                    independent = core.get("independent")
                    reflection_probe_lines = (family15_probe_lines(independent)
                                              if isinstance(independent, Mapping) else [])
                except (CoreRawUnavailable, ValueError):
                    reflection_probe_lines = []  # the existing independent/check read state reports the failure
                lines.extend([*_family_15_lines(data.get("lab_independent"), data.get("lab_independent_reason"),
                                             checks=data.get("lab_checks"),
                                             checks_state=data.get("lab_checks_state", "present")),
                              *reflection_probe_lines,
                              *_roles_lines(core), ""])
            else:
                lines.extend(_gate_lines(core))
                lines.extend(_family_14_lines(core))
            suspension = last_suspension(core)
            if suspension is not None:
                hint = suspension_hint(suspension["reason"], "ru",
                                       ((core.get("connection") or {}).get("detail") or {}).get("code"))
                lines.extend([f"Кампания приостановлена: {_cell(suspension['reason'])} "
                              f"(владелец {_cell(suspension['owner'])}, evidence {_cell(suspension['evidence_kind'])}).",
                              *([hint] if hint is not None else []),
                              ("Прогон legacy runner продолжается `resume_native` в той же папке."
                               if data.get("legacy_run") else
                               self._owner_continuation()
                               if data.get("owner_run") else
                               "Продолжение средствами Lab в этой версии недоступно; папка прогона сохранена."),
                              ""])
            if core.get("suspensions"):
                lines.extend(["| Запись | Причина | Владелец | Evidence |", "| --- | --- | --- | --- |"])
                for row in core["suspensions"]:
                    lines.append("| " + " | ".join(_cell(row[key]) for key in (
                        "seq", "reason", "owner", "evidence_kind")) + " |")
                window = core["indeterminate"].get("window")
                if window is not None:
                    lines.append("Окно неопределённости: " + _cell(json.dumps(window, ensure_ascii=False, sort_keys=True)))
                lines.append("")
            if core["rebases"]:
                lines.extend(["Продолжение под изменённой identity по авторизации оператора. "
                              "Lab принимает findings через `lab campaign DIR --continue --accept CODE:DIGEST --reason TEXT` "
                              "или `lab resume OPERATION --accept CODE:DIGEST --reason TEXT`.", "",
                              "| Прежняя identity | Новая identity | Версия core | Изменённые поля | Причина |",
                              "| --- | --- | --- | --- | --- |"])
                for rebase in core["rebases"]:
                    lines.append("| " + " | ".join(_cell(x) for x in (
                        rebase["previous_digest"], rebase["identity_digest"], rebase["producing_version"],
                        ", ".join(rebase["changed_fields"]), rebase["reason"])) + " |")
                lines.append("")
            header = ("| Раунд | Попытка | Исход | Причина | " + ("" if family_15 else "Гейт | Фокус | ")
                      + "Артефакт | Измерение baseline | Измерение попытки |")
            lines.extend(["Исходная версия: " + _fields(core["initial_champion"]),
                          "Текущая версия: " + _fields(core["final_champion"]), "",
                          header, "| " + " | ".join("---" for _ in range(header.count("|") - 1)) + " |"])
            for row in core["rounds"]:
                for attempt in row["attempts"]:
                    gate = () if family_15 else _gate_cells(attempt)
                    lines.append("| " + " | ".join(_cell(x) for x in (
                        row["round"], attempt["attempt_id"], attempt["outcome"], attempt["reason"], *gate,
                        attempt["artifact_locator"], row.get("baseline_measurement_id"), attempt.get("measurement_id")
                    )) + " |")
            if empty:
                reflections = {r["round"]: r["reflection"] for r in self.data.get("lab_rounds") or ()}
                lines.extend(["", "Раунды без попыток:", "", "| Раунд | Исход | Причина | Класс ошибки рефлексии |",
                              "| --- | --- | --- | --- |"])
                for row in empty:
                    reflection = reflections.get(row["round"]) or {}
                    lines.append("| " + " | ".join(_cell(x) for x in (
                        row["round"], row["outcome"], row.get("reason"), reflection.get("error_class"))) + " |")
                if unread:
                    lines.extend(["", unread])
            finals = [] if family_15 else _final_lines(core["rounds"])
            if finals:
                lines.extend(["", *finals])
            lines.extend(["", "Изменения кандидата: см. закреплённые артефакты; текстовый diff не вычислен.", ""])
        if data.get("lab_result") is not None:
            lines.extend(["Остановка / завершение Lab: " + _fields(data["lab_result"]), ""])
        if data["producing_version"] is not None:
            lines.extend(["Producing evo-skills: " + _cell(data["producing_version"]), ""])
        if not data["distributions"]:
            lines.extend(["Хеши producing wheels: не переданы.", ""])
        else:
            for pin in data["distributions"]:
                lines.append(_cell(pin["name"]) + " " + _cell(pin["version"]) + ": " + _cell(pin["sha256"]))
            lines.append("")
        for measurement in data["measurements"]:
            coverage = measurement["coverage"]
            expected = "неизвестно" if coverage["expected"] is None else str(coverage["expected"])
            lines.extend(["## Измерение " + _cell(measurement["run_id"]), "",
                          "Версия: " + _cell(measurement["candidate_digest"]), "",
                          f"Покрытие: {coverage['scored']} / {expected} ({coverage['state']})."])
            if measurement.get("unanswered_state") in {"present", "partial"}:
                lines.append(f"без ответа: {measurement['unanswered']} из {coverage['scored']}.")
            if data["operation"] == "evaluation":
                lines.append(f"Оценено: {coverage['scored']} из {expected}.")
                result = data.get("lab_result") or {}
                unmeasured = result.get("unmeasured")
                count = len(unmeasured) if isinstance(unmeasured, list) else "неизвестно"
                lines.append(f"Неоценённых сэмплов: {count}; stopped_reason: {_cell(result.get('stopped_reason', 'unknown'))}; "
                             f"min_coverage: {_cell(result.get('min_coverage', 1.0))}.")
                for sample in unmeasured or ():
                    lines.append("- " + ", ".join(f"{key}={_cell(sample[key])}" for key in
                                 ("case_id", "sample_id", "outcome", "tries")))
            if measurement["tries"] is not None:
                lines.append(f"Попыток исполнения: {measurement['tries']}.")
            if measurement.get("call_failures_by_code"):
                lines.append("Сбои вызовов цели по кодам: " + ", ".join(
                    f"{_cell(code)} — {count}" for code, count in measurement["call_failures_by_code"].items()) + ".")
            if measurement.get("call_retries") is not None:
                lines.append("Повторы вызовов: " + _cell(json.dumps(measurement["call_retries"], sort_keys=True)) + ".")
            excluded = measurement["excluded_pairs"]
            if excluded is not None:
                lines.extend([f"Исключено базовых пар: {excluded['count']} (исчерпали повторы; "
                              "в ожидаемое покрытие не входят).", "",
                              "| Сценарий | Повтор | Владелец | Причина | Попыток |", "| --- | --- | --- | --- | --- |"])
                for pair in excluded["pairs"]:
                    lines.append("| " + " | ".join(_cell(pair[key]) for key in (
                        "case_id", "sample_id", "owner", "reason", "tries")) + " |")
                if excluded["truncated"]:
                    lines.append("Список пар усечён core; полное число — выше.")
                lines.append("")
            lines.extend(["Целостность журнала: " + _cell(measurement["journal_integrity"]), "",
                          "| Метрика | Среднее измеренных значений | Число оценок |", "| --- | --- | --- |"])
            for name, metric in measurement["metrics"].items():
                lines.append(f"| {_cell(name)} | {metric['mean']:.6g} | {metric['count']} |")
            if not measurement["metrics"]:
                lines.extend(["", "Оценки отсутствуют; ошибки не заменены нулевыми баллами."])
            lines.extend(["", "| Сценарий | Повтор | Исполнение | Оценивание |", "| --- | --- | --- | --- |"])
            for sample in measurement["samples"]:
                execution = sample["execution"]
                outcome = execution["outcome"] if execution else sample["execution_state"]
                lines.append("| " + " | ".join(_cell(x) for x in (sample["case_id"], sample["sample_id"], outcome, sample["scoring_state"])) + " |")
            lines.append("")
        if not data["measurements"]:
            lines.extend(["Доступных измерений нет; покрытие не подтверждено.", ""])
        if data["unavailable_measurements"]:
            lines.extend(["Недоступные измерения: " + ", ".join(_cell(key) for key in data["unavailable_measurements"]), ""])
        cost = data["cost"]
        if cost is None:
            lines.append("Расход: неизвестен (данные учёта не переданы).")
        else:
            amount = Decimal(cost["upper_units"]) / Decimal(1000000000)
            limit = Decimal(cost["limit_units"]) / Decimal(1000000000)
            label = "учтённый расход; границы резерва нарушены" if cost["overrun_requests"] else "верхняя оценка по тарифу"
            lines.extend([f"Расход: {label} {amount} {cost['currency']}; лимит {limit} {cost['currency']}.",
                          f"Запросов: {cost['requests']}; неизвестный расход / в полёте: {cost['unknown_requests']}; отказов бюджета: {cost['refusals']}; превышений резервации: {cost['overrun_requests']}; выходов за объявленные границы: {cost['bound_observations']}.",
                          "Эта оценка не является подтверждённым счётом провайдера."])
        return "\n".join(lines) + "\n"

    def write(self, directory: Path) -> None:
        """Publish only into a fresh, private report directory."""
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name, text in (("report-data.json", self._encoded + "\n"), ("report.md", self.markdown())):
            path = directory / name
            with path.open("x", encoding="utf-8") as stream:
                stream.write(text)
            path.chmod(0o600)


def _with_lab_result(report: Report, lab_result: Mapping[str, Any]) -> Report:
    """Replace only Lab evidence while preserving the core reader's projection."""
    data = report.data
    data["lab_result"] = dict(lab_result)
    return Report(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False))


class _LabResultInvalid(ValueError):
    """A legacy Lab result keeps its ValueError family and typed provenance."""


class _LabResultIOError(OSError):
    """A legacy Lab result keeps its OSError family and typed provenance."""


def build_report(
    projection: Projection | CampaignProjection,
    *,
    measurements: Mapping[str, Projection] | None = None,
    expected_samples: Mapping[str, int] | None = None,
    cost: CostSummary | None = None,
    distributions: Sequence[WheelPin] = (),
    producing_version: str | None = None,
    comparison_scope: str = "unspecified",
    lab_result: Mapping[str, Any] | None = None,
    lab_notices: Mapping[str, Any] | None = None,
    unanswered: Mapping[str, Mapping] | None = None,
) -> Report:
    if not isinstance(projection, (Projection, CampaignProjection)):
        raise TypeError("report requires a validated core projection")
    source = projection.to_payload()
    campaign = isinstance(projection, CampaignProjection)
    if campaign and source["kind"] != "campaign_result":
        raise ValueError("campaign report requires a core result projection")
    if comparison_scope not in {"in-sample", "unspecified", "not-applicable"}:
        raise ValueError("unsupported comparison scope")
    expected_samples = expected_samples or {}
    if campaign:
        declared = {row["measurement_id"]: row for row in source["measurements"]}
        supplied = measurements or {}
        if set(supplied) - set(declared):
            raise ValueError("measurement is not part of this campaign")
        for key, measurement in supplied.items():
            if measurement.to_payload()["run_id"] != key:
                raise ValueError("measurement identity differs from campaign")
            if measurement.to_payload()["candidate_digest"] != declared[key]["candidate_digest"]:
                raise ValueError("measurement candidate differs from campaign")
    else:
        if measurements:
            raise ValueError("standalone report cannot combine unrelated measurements")
        supplied = {source["run_id"]: projection}
    family = ({key: row for key, row in declared.items() if "tries" in row}
              if campaign else {})
    data = {
        "operation": "campaign" if campaign else "evaluation", "core": source,
        "lab_result": dict(lab_result) if lab_result is not None else None,
        "comparison_scope": comparison_scope, "producing_version": producing_version,
        "distributions": [pin.public() for pin in distributions],
        "measurements": [_measurement(value, expected_samples.get(key), family.get(key))
                         for key, value in supplied.items()],
        "unavailable_measurements": sorted(set(declared) - set(supplied)) if campaign else [],
        "cost": None if cost is None else asdict(cost),
    }
    for measurement in data["measurements"]:
        measurement.update((unanswered or {}).get(measurement["run_id"],
            {"unanswered": None, "unanswered_state": "unavailable", "answered_reported": None}))
    if not campaign:
        # An open report body: additive fields, outer owner-report/13 unchanged.
        data.update(lab_notices or {"lab_notices": [], "lab_notices_state":
            "unavailable" if lab_result is not None and any(
                sample.get("scoring_state") == "finished" for sample in source.get("samples") or ()) else "present"})
    if not campaign and lab_result is not None:
        from evolution_lab._outcomes import validate_coverage_threshold
        threshold = validate_coverage_threshold(lab_result.get("min_coverage", 1.0))
        for measurement in data["measurements"]:
            coverage = measurement["coverage"]
            expected = coverage["expected"]
            if (expected and coverage["scored"] / expected >= threshold
                    and lab_result.get("stopped_reason") is None
                    and (lab_result.get("persistence") or {"state": "saved"}).get("state") == "saved"
                    and lab_result.get("coverage") == {"expected": expected, "scored": coverage["scored"]}
                    and coverage["scored"] < expected):
                coverage["state"] = "accepted"
    if campaign:
        from evolution_lab._reflector_probe import probe_fields
        data.update(probe_fields())
    return Report(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _read_lab_result(path: Path):
    try:
        if path.is_symlink():
            raise _LabResultInvalid("invalid Lab result file")
        if not path.exists():
            return None
        # Campaign process/supervisor receipts retain their existing 64 KiB cap.
        limit = MAX_EVALUATION_RESULT_BYTES if path.name == "evaluation-result.json" else 65536
        if path.stat().st_size > limit:
            raise _LabResultInvalid("invalid Lab result file")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise _LabResultInvalid("invalid Lab result")
        return value
    except _LabResultInvalid:
        raise
    except OSError as exc:
        raise _LabResultIOError(*exc.args) from exc
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _LabResultInvalid("invalid Lab result") from exc


_START_DIR = re.compile(r"start-([2-9]|[1-9][0-9]+)")


def _campaign_lab_results(logs: Path) -> tuple[dict | None, list[dict]]:
    """Latest Lab process result and the ordered history of every start (ADR-0010)."""
    if logs.is_symlink():
        raise _LabResultInvalid("invalid Lab result file")
    if not logs.is_dir():
        return None, []
    starts = [(1, logs, False)]
    for entry in logs.iterdir():
        match = _START_DIR.fullmatch(entry.name)
        if match is None:
            continue
        if entry.is_symlink() or not entry.is_dir():
            raise _LabResultInvalid("invalid Lab result file")
        starts.append((int(match[1]), entry, True))
    latest, history = None, []
    for number, directory, resume in sorted(starts):
        result = _read_lab_result(directory / "process-result.json")
        supervisor = _read_lab_result(directory / "supervisor-result.json") or {}
        if result is not None:
            latest = result
            exit_code = result.get("exit_code")
            caller_lost = result.get("caller_lost", supervisor.get("caller_lost"))
        else:
            exit_code = supervisor.get("core_exit_code")
            caller_lost = supervisor.get("caller_lost")
        history.append({
            "start": number, "resume": resume, "exit_code": exit_code,
            "core_refused": None if exit_code is None else exit_code == 2,
            "caller_lost": caller_lost,
            "process_result": result is not None,
        })
    return latest, history


def read_evaluation_report(run_directory: Path, **kwargs: Any) -> Report:
    root = Path(run_directory)
    lab_result = _read_lab_result(root / "evaluation-result.json")
    if (root.parent / "evaluation-inputs.json").is_file():
        lab_result = _read_lab_result(root.parent / "evaluation-result.json") or lab_result
    snapshot = inspect_journal(run_directory)
    if not snapshot.records:
        raise ValueError("evaluation journal has no validated identity")
    first = snapshot.records[0].payload
    projection = project_result(snapshot)
    lab_result = kwargs.pop("lab_result", lab_result)
    from evolution_lab.owner.measurements import evaluation_focus_notices
    if "lab_notices" not in kwargs:
        kwargs["lab_notices"] = evaluation_focus_notices(projection.to_payload(),
            root, root / "events.jsonl", terminal=lab_result is not None)
    if "unanswered" not in kwargs:
        from evolution_lab.owner.measurements import unanswered_count
        payload = projection.to_payload()
        value = unanswered_count(payload.get("samples") or [], root, root / "events.jsonl", payload["run_id"])
        kwargs["unanswered"] = {payload["run_id"]: value}
    return build_report(projection, expected_samples={projection.to_payload()["run_id"]: len(first["identity"]["schedule"])},
                        producing_version=first["producing_version"], lab_result=lab_result, **kwargs)


def _lab_fields(root: Path, core: Mapping[str, Any], family: int = 14, read: CampaignRead | None = None,
                *, baseline_cache=None) -> dict:
    """``lab_rounds`` and ``lab_notices`` of a campaign report (spec dev46 §4.2): the round reflections come from the
    ``core_raw`` adapter through the view's own builder (``round_reflections``) and keep codes only; the notice is the
    view's ``lab_notices`` over them, so the view, the report and ``report.md`` give one notice. A failed read is an
    explicit state next to the empty value (ADR-0019 p.3): ``lab_rounds_state`` ``present``/``unavailable`` and
    ``lab_rounds_reason``, the adapter's code (``unreadable`` for any other failure). Family 15 groups its notice by
    ``attempt15_group``; family 14's final decisions are not read for it."""
    from evolution_lab.owner.campaign_views import (final_decisions, lab_notices, reflection_codes,
                                                    round_reflections)
    from evolution_lab.owner.core_raw import CoreRawUnavailable, campaign_details
    core_rounds = core.get("rounds") or []
    reason = None
    try:
        decisions = None if family == 15 else final_decisions(core)
    except Exception:  # blocks the pin does not describe: a finalist without a decision is `unknown`, no notice
        decisions = None
    try:
        details = campaign_details(root, read=read) if family == 15 and read is not None else campaign_details(root)
        reflections = round_reflections(core_rounds, details["rounds"])
    except CoreRawUnavailable as error:
        if error.code == "core_raw_unsupported":
            raise  # the report already needs the adapter (campaign_log); no fallback path (spec dev46 4.2)
        reflections, reason = None, error.code  # unreadable journal is a data fault, not a missing adapter
    except Exception:
        reflections, reason = None, "unreadable"
    from evolution_lab.owner.campaign_views import baseline_notice_fields
    baseline = baseline_notice_fields(root, getattr(getattr(read, "view", None), "snapshot", None),
                                      cache=baseline_cache, read=read)
    return {"lab_notices_state": baseline["lab_notices_state"],
            "lab_rounds": None if reflections is None else
            [{"round": n, "reflection": reflection_codes(r), "source": "core_raw"} for n, r in reflections.items()],
            "lab_rounds_state": "present" if reflections is not None else "unavailable",
            "lab_rounds_reason": reason,
            "lab_notices": lab_notices(core.get("campaign_outcome"), core_rounds, reflections,
                                       decisions=decisions, family=family) + baseline["lab_notices"]}


def _lab_independent(core: Mapping[str, Any], read: CampaignRead) -> dict:
    """``lab-owner-report/15`` embeds ``lab-campaign-view/15``'s own ``independent`` block of Core's authenticated
    family-15 projection, grouped with the rounds Core closed and its recorded terminal, as the view groups it
    (``campaign_views._view_15``). A block or rational this pin does not describe is null, as in the view, never a
    failed report; a failed read is an explicit state next to the empty value (ADR-0019 p.3), with the view's words
    (``details_state``/``details_reason``, ``details_unavailable("unreadable")``): ``lab_independent_state``
    ``present``/``unavailable`` and ``lab_independent_reason`` null/``unreadable``."""
    from evolution_lab.owner.independent_view import campaign_finished, closed_rounds, independent_view
    from evolution_lab.owner.core_raw import family15_checks
    try:
        checks = {"lab_checks": family15_checks(core["independent"]),
                  "lab_checks_state": "present", "lab_checks_reason": None}
    except Exception:
        checks = {"lab_checks": None, "lab_checks_state": "unavailable", "lab_checks_reason": "unreadable"}
    try:
        if core["independent"]["attempts"] and read.bindings_reason is not None:
            raise ValueError("binding publications unreadable")
        block = independent_view(core["independent"], closed_rounds=closed_rounds(core.get("rounds")),
                                 campaign_finished=campaign_finished(core), bindings=read.bindings, stages=read.stages)
    except Exception:
        return {**checks, "lab_independent": None, "lab_independent_state": "unavailable",
                "lab_independent_reason": "unreadable"}
    return {**checks, "lab_independent": block, "lab_independent_state": "present", "lab_independent_reason": None}


def read_campaign_report(run_directory: Path, *, owner_run: bool = False,
                         owner_resume_target: dict | None = None, owner_reflector_probe: dict | None = None,
                         families: tuple[int, ...] = LEGACY_FAMILIES, read: CampaignRead | None = None,
                         baseline_cache=None, **kwargs: Any) -> Report:
    """One Core read (``campaign_read``, spec 2026-10-19 D3): H1 keeps the default family 14; Owner passes
    ``core_raw.FAMILIES`` and its own ``read`` (``campaign_read(..., with_result=True)``), the one acquisition its
    status metadata and usage also come from; without one the report makes it. For family 15 the result carries
    Core's authenticated ``independent`` projection, and the report data gains ``lab_independent``, the view's block
    of it, with ``lab_independent_state``/``lab_independent_reason``; family-14 data is unchanged."""
    root = Path(run_directory)
    if read is None:
        read = campaign_read(root, families=families, with_result=True)
    elif read.result is None or read.family not in families:
        raise ValueError("the shared campaign read has no result of these families")
    view = read.view
    from evolution_lab.owner.core_raw import campaign_log
    if read.family == 15 and read.records:
        records, integrity = read.records, read.journal_integrity
    else:
        log = campaign_log(root, families=families)
        records, integrity = log.records, log.journal_integrity
    if not records:
        raise ValueError("campaign journal has no validated identity")
    projection = read.result
    measurements, expected = {}, {}
    for entry in projection.to_payload()["measurements"]:
        key = entry["measurement_id"]
        if view.siblings.get(key) != "born":
            continue
        snapshot = inspect_journal((root / entry["locator"]).parent)
        measurements[key] = project_result(snapshot)
        expected[key] = len(snapshot.records[0].payload["identity"]["schedule"])
    if "unanswered" not in kwargs:
        from evolution_lab.owner.measurements import unanswered_count
        kwargs["unanswered"] = {entry["measurement_id"]: unanswered_count(
            measurements[entry["measurement_id"]].to_payload().get("samples") or [], root,
            root / entry["locator"], entry["measurement_id"])
            for entry in projection.to_payload()["measurements"] if entry["measurement_id"] in measurements}
    logs = root.with_name(root.name + "-lab-logs")
    lab_result, lab_starts = _campaign_lab_results(logs)
    report = build_report(projection, measurements=measurements, expected_samples=expected,
                          producing_version=records[0].payload["producing_version"],
                          lab_result=lab_result, **kwargs)
    data = report.data
    from evolution_lab._reflector_probe import probe_fields
    data.update(probe_fields(owner_reflector_probe))
    data["campaign_journal_integrity"] = integrity
    data["lab_starts"] = lab_starts
    data.update(_lab_fields(root, data.get("core") or {}, read.family, read, baseline_cache=baseline_cache))
    if read.family == 15:
        data.update(_lab_independent(data["core"], read))
    # Only `run_native` takes the supervisor lock (`runner.py`); the H1 client
    # also creates a `-lab-logs` directory, so `lab_starts` alone cannot tell
    # a legacy run from one `resume_native` would refuse (`not_a_lab_campaign`).
    data["legacy_run"] = (logs / "supervisor.lock").is_file()
    if owner_run:
        data["owner_run"] = True
    if owner_resume_target is not None:
        data["owner_resume_target"] = owner_resume_target
    return Report(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False))
