"""Statements of the family-15 ``independent`` block, shared by the CLI (English) and ``report.md`` (Russian).

One rule per statement, two languages, as ``_suspension.suspension_hint`` does for the hints. Each scope reads by
its own ``display`` (computed once in Owner, spec 2026-10-19 Q5): the point estimate, the one-sided lower bound L with
its confidence, the threshold. ``enclosure.upper`` is never shown: it brackets L's computation, it is not an upper
confidence bound. Every comparison is against the round's champion; only its measurement differs (Q2 as corrected),
so no label names a Parent. Numbers are ``{value, exact}`` and only ``value`` is read, for display: Lab compares and
recomputes nothing (tenet ``one-campaign-authority``). Core strings go through the caller's ``escape``.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

_TEXT: Mapping[str, Mapping[str, Any]] = {
    "en": {
        "out_of_range": "outside display range (exact value kept)",
        "threshold": "threshold {op} {value}",
        "bound": "gain {gain} (estimate); with {confidence} confidence not below {lower}; {threshold}",
        "bootstrap": "gain {gain} (estimate) · empirical one-sided repeat-bootstrap {confidence}, lower {lower} · {threshold}",
        "bootstrap_label": "empirical one-sided repeat-bootstrap lower bound",
        "variance_label": "Candidate primary-score repeat variance (score squared)",
        "variance_cases": "cases with variance",
        "variance_missing": "unavailable cases",
        "variance_legacy": "details unavailable in this report",
        "point": "gain {gain}; {threshold}",
        "no_threshold": "gain {gain} · no threshold",
        "uncalibrated": "threshold not calibrated yet",
        "fallback": "weaker: point decision instead of a bound ({reason})",
        "reference": "vs round champion · {measurement}",
        "measurement": {"stored": "stored measurement", "fresh": "fresh measurement",
                        "unknown": "measurement not recorded"},
        "preflight_bound": "{name} maximal confidence {c_max} vs requested {confidence} ({verdict})",
        "preflight_point": "{name} maximal gain {delta_max} vs {threshold} ({verdict})",
        "verdict": {True: "can pass", False: "cannot pass"},
    },
    "ru": {
        "out_of_range": "вне диапазона отображения (точное значение сохранено)",
        "threshold": "порог {op} {value}",
        "bound": "прирост {gain} (оценка); с уверенностью {confidence} не ниже {lower}; {threshold}",
        "bootstrap": "прирост {gain} (оценка) · односторонний эмпирический repeat-bootstrap {confidence}, нижняя граница {lower} · {threshold}",
        "bootstrap_label": "односторонняя эмпирическая нижняя граница repeat-bootstrap",
        "variance_label": "Дисперсия повторов основной метрики кандидата (в квадрате балла)",
        "variance_cases": "сценариев с дисперсией",
        "variance_missing": "сценарии без оценки дисперсии",
        "variance_legacy": "нет подробностей в этом отчёте",
        "point": "прирост {gain}; {threshold}",
        "no_threshold": "прирост {gain}; порога нет",
        "uncalibrated": "порог ещё не откалиброван",
        "fallback": "слабее: точечное решение вместо границы ({reason})",
        "reference": "против чемпиона раунда · {measurement}",
        "measurement": {"stored": "сохранённый замер", "fresh": "свежий замер", "unknown": "замер не записан"},
        "preflight_bound": "{name}: наибольшая уверенность {c_max} при запрошенной {confidence} ({verdict})",
        "preflight_point": "{name}: наибольший прирост {delta_max}; {threshold} ({verdict})",
        "verdict": {True: "может пройти", False: "не может пройти"},
    },
}


def _printable(value: Any) -> str:
    return "".join(ch if ch.isprintable() else "?" for ch in str(value))


def stages_text(stages: list[dict]) -> str:
    """Restate Owner's ordered stage facts, shared by CLI and Markdown."""
    return "stages: " + " · ".join(f"{stage['scope']} {stage['state']}" for stage in stages)


def round_progress_text(row: Mapping[str, Any], lang: str, escape: Callable[[Any], str]) -> list[str]:
    number = escape(row["round"])
    head = (
        f"Раунд {number} · открыто попыток {row['attempts_opened']} · кандидатов зафиксировано {row['candidates_sealed']} · попыток с оценкой кандидата {row['attempts_measured']}"
        if lang == "ru" else
        f"round {number} · {row['attempts_opened']} Attempts opened · {row['candidates_sealed']} Candidates sealed · {row['attempts_measured']} Attempts with a Candidate score"
    )
    if row["outcome"] is not None:
        head += f" · {escape(row['outcome'])}"
    if row["reason"] is not None:
        head += f" ({escape(row['reason'])})"
    role = "Рефлексия" if lang == "ru" else "Reflection"
    suspended = "зафиксирована приостановка" if lang == "ru" else "suspension recorded"
    lines = [head]
    for reflection in row["reflections"]:
        line = f"{role} · {escape(reflection['status'])}"
        if reflection["reason"] is not None:
            line += f" · {escape(reflection['category'])} · {escape(reflection['reason'])}"
        if reflection["suspended"]:
            line += f" · {suspended}"
        lines.append(line)
    return lines


def attempt_progress_text(row: Mapping[str, Any], lang: str, escape: Callable[[Any], str]) -> list[str]:
    editor, candidate, parent = (
        ("Редактор", "Замер кандидата", "Свежий замер чемпиона") if lang == "ru"
        else ("Editor", "Candidate measurement", "Fresh Champion measurement")
    )
    sealed = ("кандидат зафиксирован" if row["candidate_sealed"] else "кандидат не зафиксирован") if lang == "ru" else (
        "Candidate sealed" if row["candidate_sealed"] else "Candidate not sealed"
    )
    head = f"{editor} {escape(row['editor'])} · {sealed} · {candidate} {escape(row['measurement'])}"
    if "parent_measurement" in row:
        head += f" · {parent} {escape(row['parent_measurement'])}"
    return [head, *(f"{editor} · failed · {escape(failure['category'])} · {escape(failure['reason'])}"
                    for failure in row["editor_failures"])]


def repeat_variance_text(noise: Mapping[str, Any], lang: str, escape: Callable[[Any], str]) -> str | None:
    if noise.get("kind") != "observed":
        return None
    words = _TEXT[lang]
    label = words["variance_label"]
    variance = noise.get("repeat_variance")
    if variance is None:
        return f"{label} · {words['variance_legacy']}"
    lo, hi = variance["minimum"], variance["maximum"]
    line = f"{label} · {noise['observed_cases']} {words['variance_cases']}"
    if lo is not None:
        interval = quantity(lo, lang) if lo["exact"] == hi["exact"] else f"{quantity(lo, lang)} – {quantity(hi, lang)}"
        line += f" · {interval}"
    if variance["unavailable_counts"]:
        counts = ", ".join(f"{escape(reason)} {count}" for reason, count in variance["unavailable_counts"].items())
        line += f" · {words['variance_missing']} · {counts}"
    return line


def _value(q: Any, lang: str) -> float | str:
    """``q['value']`` when it is a number; the out-of-range words for Core's exact value outside binary64; ``-``."""
    if not isinstance(q, dict):
        return "-"
    value = q.get("value")
    if value is None:
        return _TEXT[lang]["out_of_range"]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "-"
    return value


def quantity(q: Any, lang: str = "en") -> str:
    """A ``{value, exact}`` quantity for reading: ``value`` only, at least two and at most four decimals."""
    value = _value(q, lang)
    if isinstance(value, str):
        return value
    if value != 0 and abs(value) < 0.0001:
        return f"{value:.2e}"
    whole, _, decimals = f"{value:.4f}".partition(".")
    decimals = decimals.rstrip("0").ljust(2, "0")
    return f"{whole}.{decimals}"


def percent(q: Any, lang: str = "en") -> str:
    value = _value(q, lang)
    return value if isinstance(value, str) else f"{value * 100:.4g}%"


def threshold_text(scope: Mapping[str, Any], lang: str = "en") -> str:
    if scope.get("threshold") is None and scope.get("rule") != "off":
        return _TEXT[lang]["uncalibrated"]
    return _TEXT[lang]["threshold"].format(op=">" if scope.get("strict") is True else "≥",
                                           value=quantity(scope.get("threshold"), lang))


def scope_statement(scope: Mapping[str, Any], lang: str = "en", escape: Callable[[Any], str] = _printable) -> str:
    """One scope by its ``display``; ``status`` shows Core's status and ``unavailable`` code, no numbers."""
    display = scope.get("display")
    if display == "status":
        code = scope.get("unavailable")
        text = escape(scope.get("status")) + (f" ({escape(code)})" if code else "")
        if scope.get("rule") == "bootstrap":
            text = f"{_TEXT[lang]['bootstrap_label']} · {text}"
        if scope.get("threshold") is None and scope.get("rule") != "off":
            text += "; " + threshold_text(scope, lang)
    elif display not in ("bound", "point", "bootstrap", "no_threshold"):
        return escape(display)
    else:
        if display == "no_threshold" and scope.get("rule") != "off" and scope.get("threshold") is None:
            display = "point"
        text = _TEXT[lang][display].format(
            gain=quantity(scope.get("point"), lang), threshold=threshold_text(scope, lang),
            confidence=percent(scope.get("confidence"), lang),
            lower=quantity(scope.get("lower") if display == "bootstrap"
                           else (scope.get("enclosure") or {}).get("lower"), lang))
    if scope.get("fallback") is not None:
        text += "; " + _TEXT[lang]["fallback"].format(reason=escape(scope["fallback"]["reason"]))
    return text


def reference_text(comparison: Mapping[str, Any], lang: str = "en") -> str:
    words = _TEXT[lang]["measurement"]
    return _TEXT[lang]["reference"].format(measurement=words.get(comparison.get("reference"), words["unknown"]))


def preflight_parts(explanation: Mapping[str, Any], lang: str = "en",
                    escape: Callable[[Any], str] = _printable) -> list[str]:
    """Core's preflight ceilings per calculated scope (a score-range ceiling, not a prediction) with Core's own
    ``passes``: ``c_max`` against the requested confidence for a ``bound`` scope, ``delta_max`` against the
    threshold otherwise. Skipped scopes have no ceilings."""
    text, parts = _TEXT[lang], []
    for scope in explanation.get("scopes") or ():
        if not isinstance(scope, dict):
            continue
        if scope.get("status") == "uncalibrated":
            parts.append(escape(scope.get("name")) + ": " + text["uncalibrated"])
            continue
        if scope.get("status") != "calculated":
            continue
        template = text["preflight_bound" if scope.get("rule") == "bound" else "preflight_point"]
        parts.append(template.format(
            name=escape(scope.get("name")), c_max=percent(scope.get("c_max"), lang),
            confidence=percent(scope.get("confidence"), lang), delta_max=quantity(scope.get("delta_max"), lang),
            threshold=threshold_text(scope, lang), verdict=text["verdict"].get(scope.get("passes"), "-")))
        if scope.get("fallback") is not None:
            parts[-1] += "; " + text["fallback"].format(reason=escape(scope["fallback"]["reason"]))
    return parts


def with_checks(lines: list[str], checks: Mapping[str, Any] | None, state: str = "present",
                escape: Callable[[Any], str] = str) -> list[str]:
    """Core report_lines order: modes, independent header, then each shadow count; no totals."""
    if state == "unavailable":
        return ["checks: unavailable (unreadable)", *lines]
    if checks is None:
        return lines
    modes = checks["modes"]
    before = ["checks: " + ", ".join(f"{escape(name)}={escape(mode)}" for name, mode in modes.items())] if modes else []
    hits = [f"shadow hits: {escape(name)} · count {count}" for name, count in checks["shadow_hits"].items()]
    return [*before, *lines[:1], *hits, *lines[1:]]
