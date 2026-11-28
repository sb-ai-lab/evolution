"""The latest suspension of a family-7 campaign projection (PDR-0014).

Lab shows core's suspension as it is: no list of reasons of its own (only repair hints for role,
scorer, final and Champion suspensions), and no continuation for Owner/service/H1 runs in this version.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_KEYS = ("seq", "reason", "owner", "evidence_kind")


def last_suspension(core: Any) -> dict[str, Any] | None:
    if not isinstance(core, Mapping) or core.get("suspended") is not True:
        return None
    rows = core.get("suspensions")
    # `core` is either a plain JSON payload (`inspect_campaign(...).to_payload()`,
    # lists) or the frozen in-process form Owner keeps on `OperationResult.core`
    # (`_freeze_json` turns lists into tuples), so accept either shape here.
    if not isinstance(rows, (list, tuple)) or not rows or not isinstance(rows[-1], Mapping):
        return None
    return {key: rows[-1].get(key) for key in _KEYS}


# Role suspensions (core a21, spec 2026-09-30 §2.4): the repair phrase only. How to continue
# depends on the path (Owner `lab resume`, legacy `resume_native`, H1 none) and is the next report/CLI line.
# `*_provider_failed` is the `provider` failure class; `*_failing` is the `unknown` class (core
# `role_failures.py`: timeout, crash, program/agent error, preflight, exception), and the Reflector has a
# second trigger: two skipped groups in a row (`skip_chain_breaks`), each skipped after
# `reflector.max_consecutive_failures` content failures. An Editor content failure is not a suspension at
# all: it closes the Attempt (`abandoned/editor_failed`). Only external causes are repairable before a
# resume: network, the provider, an agent CLI's login, host resources, a transient crash or timeout. The
# interpreter, installed tools and runtime files belong to the Bundle's captured execution environment, and
# Lab re-captures it on every start (`environment_invalid` on a change); the role's program, prompt and
# limits live in the Bundle and the campaign identity. So changing any of them needs a new Bundle and a new
# campaign.
ROLE_SUSPENSIONS: Mapping[str, tuple[str, str]] = {
    "reflector_provider_failed": (
        "the Reflector's provider failed or refused; check the provider's availability, key and quota",
        "провайдер Reflector недоступен или отказал — проверьте доступность провайдера, ключ и квоту"),
    "reflector_failing": (
        "the Reflector keeps failing outside its diagnosis (a timeout, a crash, network, host resources or "
        "an agent CLI's login), or two error groups in a row were skipped after repeated content failures; "
        "after an external or transient failure, fix it and continue; after repeated content skips "
        "continuing helps only if the failures were transient, otherwise the Reflector's program, prompt, "
        "limits or max_consecutive_failures must change, which needs a new Bundle and a new campaign",
        "Reflector раз за разом падает вне своего диагноза (таймаут, сбой процесса, сеть, ресурсы хоста, "
        "логин CLI агента) либо две группы ошибок подряд пропущены после повторных сбоев по содержанию; "
        "после внешнего или временного сбоя устраните его и продолжайте; после повторных пропусков по "
        "содержанию продолжение поможет, только если сбои были временными, иначе нужно менять программу, "
        "промпт, лимиты Reflector или max_consecutive_failures — это новый Bundle и новая кампания"),
    "editor_provider_failed": (
        "the Editor's provider failed or refused; check the provider's availability, key and quota",
        "провайдер Editor недоступен или отказал — проверьте доступность провайдера, ключ и квоту"),
    "editor_failing": (
        "the Editor keeps failing outside its edit (a timeout, a crash or an external problem such as "
        "network, host resources or an agent CLI's login); fix the external cause and continue - changing "
        "the Editor's program, prompt, limits or environment needs a new Bundle and a new campaign",
        "Editor раз за разом падает вне своей правки (таймаут, сбой процесса или внешняя проблема — сеть, "
        "ресурсы хоста, логин CLI агента); устраните внешнюю причину и продолжайте — смена программы, "
        "промпта, лимитов или окружения Editor — это новый Bundle и новая кампания"),
}


# Scorer suspensions with `fault_rows` evidence (core a26 #97 adds the range rule): continuing re-runs each named
# row once (a full pair: rollout and scorer, paid target calls); the same scorer gives the same row again. The
# scorer lives in the Bundle, so a fixed scorer is a new Bundle and a new campaign (spec 2026-10-03 §3).
SCORER_SUSPENSIONS: Mapping[str, tuple[str, str]] = {
    "scorer_metric_out_of_range": (
        "the scorer returned a primary metric outside [0, 1]; continuing helps only if the value was accidental "
        "(for example, a nondeterministic judge), and each continuation re-measures the named rows again with "
        "paid target calls; a fixed scorer means a new Bundle and a new campaign",
        "скорер вернул основную метрику вне [0, 1]; продолжение поможет, только если значение было случайным "
        "(например, недетерминированный судья), и каждое продолжение заново и платно перемеряет названные строки; "
        "исправленный скорер — это новый Bundle и новая кампания"),
    "scorer_metric_missing": (
        "the scorer's output lacks the primary metric; continuing helps only if this was accidental, and each "
        "continuation re-measures the named rows again with paid target calls; a fixed scorer means a new Bundle "
        "and a new campaign",
        "в выводе скорера нет основной метрики; продолжение поможет, только если это было случайно, и каждое "
        "продолжение заново и платно перемеряет названные строки; исправленный скорер — это новый Bundle и новая "
        "кампания"),
}


# Family 14 (spec 2026-10-13 §2.3, core a35 #194): a stop that a kill cut short, or that also decided a
# fault or threshold suspension, left Champion pairs unfinished; the next plain resume tries each listed pair once.
# No target requirement: the same continuation as any other suspension.
CHAMPION_SUSPENSIONS: Mapping[str, tuple[str, str]] = {
    "champion_incomplete": (
        "The Champion's measurement was left unfinished: a stop cut it short or came with another suspension. "
        "Continue the campaign with the same action on a ready target: core re-measures each Champion pair core recorded "
        "once.",
        "Замер Champion не завершён: остановка оборвала его или совпала с другой приостановкой. Продолжите "
        "кампанию тем же действием на готовой цели: core один раз перемерит записанные core пары Champion."),
}


# Family 14 (spec 2026-10-13 §6.4, PDR-0015 as amended 2026-10-14): the final measurement, the connection check and
# the Grouper. None needs a fresh target. After a connection failure, continuing helps only if the cause is outside
# the Bundle: the same Bundle repeats the error, another Bundle is refused by Lab, another identity by core.
FINAL_SUSPENSIONS: Mapping[str, tuple[str, str]] = {
    "final_incomplete": (
        "The round's final measurement was left unmeasured by an infrastructure failure. Continue the campaign with "
        "the same action on a ready target: core re-measures the missing final pairs.",
        "Финальный замер раунда не домерен из-за инфраструктуры. Продолжите кампанию тем же действием на готовой "
        "цели: core перемерит недостающие пары финала."),
    "connection_incompatible": (
        "The target's or the scorer's answer does not meet the scorer's metric_contract. If the cause is outside the "
        "Bundle (the agent's provider, the target's environment), fix it and continue with the same action. If the "
        "error is in the agent, the scorer or metric_contract (all in the Bundle), this campaign cannot continue: fix "
        "the package and start a new campaign.",
        "Ответ цели или скорера не соответствует metric_contract скорера. Если причина вне Bundle (провайдер агента, "
        "окружение цели) — устраните её и продолжите тем же действием. Если ошибка в агенте, скорере или "
        "metric_contract (всё это в Bundle) — эту кампанию продолжить нельзя: исправьте пакет и начните новую "
        "кампанию."),
    "connection_preflight_unavailable": (
        "The target or the scorer was unavailable for core's connection check. Fix it and continue the campaign with "
        "the same action.",
        "Цель или скорер были недоступны на проверке подключения ядра. Устраните это и продолжите кампанию тем же "
        "действием."),
    "connection_preflight_inconclusive": (
        "Core's connection check did not finish: the target answered, but the agent ran out of its turn or time "
        "budget or failed on the first train case. If the cause is outside the Bundle (the agent's provider, the "
        "environment), fix it and continue with the same action; if it is the campaign's budget or the agent (in the "
        "Bundle), this campaign cannot continue: fix the package and start a new campaign.",
        "Проверка подключения ядра не завершилась: цель ответила, но агент не уложился в бюджет хода или времени либо "
        "упал на первом train-кейсе. Если причина вне Bundle (провайдер агента, окружение) — устраните её и "
        "продолжите тем же действием; если это бюджет кампании или агент (в Bundle) — эту кампанию продолжить "
        "нельзя: исправьте пакет и начните новую кампанию."),
    "grouper_provider_failed": (
        "the Grouper's provider failed or refused; check the provider's availability, key and quota, then continue",
        "провайдер Grouper недоступен или отказал — проверьте доступность провайдера, ключ и квоту и продолжайте"),
    "grouper_failing": (
        "the Grouper keeps failing outside its grouping (a timeout, a crash, network or host resources); fix the "
        "external cause and continue - changing the Grouper's program, prompt or limits needs a new Bundle and a new "
        "campaign",
        "Grouper раз за разом падает вне своей группировки (таймаут, сбой процесса, сеть, ресурсы хоста); устраните "
        "внешнюю причину и продолжайте — смена программы, промпта или лимитов Grouper — это новый Bundle и новая "
        "кампания"),
}


# b10 connection preflight and later instance attestation failures. The latter is
# a persistence terminal, NOT a Core suspension; PDR-0015 still forbids Lab resume.
_DETAIL_HINTS: Mapping[tuple[str, str], tuple[str, str]] = {
    ("connection_incompatible", "connection_description_secret"): (
        "The target instance description (the agent's describe()) contains a value from the role or scorer "
        "environments; this is in the Bundle: fix describe(), build a new package and start a new campaign.",
        "Описание инстанса цели (describe() агента) содержит значение из окружений ролей или скорера; "
        "это в Bundle — исправьте describe(), соберите новый пакет и начните новую кампанию."),
    ("persistence_failed", "record_matches_secret"): (
        "The candidate instance description contains a value from the role or scorer environments; "
        "you cannot continue this campaign in Lab: fix describe(), build a new package and start a new campaign.",
        "Описание инстанса кандидата содержит значение из окружений ролей или скорера; "
        "в Lab эту кампанию продолжить нельзя: исправьте describe(), соберите новый пакет и начните новую кампанию."),
}


# Projection can fail after any recorded terminal; it does not change that terminal.
_PROJECTION_INVALID_HINT = (
    "Core's report could not be projected; the journal is intact. "
    "Install a fixed Core and read the report again.",
    "Не удалось построить отчёт Core; журнал сохранён. "
    "Установите исправленный Core и прочитайте отчёт снова.",
)
_PROJECTION_INVALID_HINTS: Mapping[str, tuple[str, str]] = {
    "finished": (
        "Core's report could not be projected; the journal is intact. "
        "Install a fixed Core and read the status with evo tree watch --once.",
        "Не удалось построить отчёт Core; журнал сохранён. "
        "Установите исправленный Core и прочитайте статус через evo tree watch --once.",
    ),
    "suspended": (
        "Core's report could not be projected; the journal is intact. "
        "Install a fixed Core, then resume with the same evo tree evolve command after one rebase.",
        "Не удалось построить отчёт Core; журнал сохранён. "
        "Установите исправленный Core, затем продолжите тем же evo tree evolve после одного rebase.",
    ),
}


def suspension_hint(reason: str, lang: str, detail: str | None = None) -> str | None:
    pair = (_PROJECTION_INVALID_HINTS.get(reason, _PROJECTION_INVALID_HINT) if detail == "projection_invalid"
            else _DETAIL_HINTS.get((reason, detail)) if detail is not None else None)
    pair = (pair or ROLE_SUSPENSIONS.get(reason) or SCORER_SUSPENSIONS.get(reason)
            or CHAMPION_SUSPENSIONS.get(reason) or FINAL_SUSPENSIONS.get(reason))
    return None if pair is None else pair[0 if lang == "en" else 1]
