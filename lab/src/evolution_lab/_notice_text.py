"""Presentation of existing Lab notices; never part of their wire form."""
from collections.abc import Mapping


def all_rounds_clean(notice: Mapping) -> bool:
    """Positive clean-only evidence from the notice producer, not an empty history.

    Core b11 campaign.py closes an empty-focus round with outcome ``clean`` and
    reason null; campaign_views.lab_notices falls back to that outcome.
    """
    reasons = notice.get("by_reason") or {}
    return (notice.get("code") == "no_candidate_measured" and notice.get("rounds", 0) > 0
            and set(reasons) == {"clean"} and reasons["clean"] > 0)


CLEAN_ROUNDS_EN = (
    "All rounds were clean: the baseline had no failures, or the scorer supplied neither verdicts nor "
    "critical_pass for completed samples. Metrics do not build campaign focus."
)
CLEAN_ROUNDS_RU = (
    "Все раунды clean: baseline без провалов, или скорер не отдаёт ни verdicts, ни critical_pass "
    "для завершённых сэмплов. Метрики не строят фокус кампании."
)


FOCUS_SIGNAL_EN = (
    "In this evaluation no admitted sample supplied verdicts or critical_pass. With this scorer campaign focus "
    "is built only from execution failures (outcome other than completed); metrics do not build campaign focus."
)
FOCUS_SIGNAL_RU = (
    "В этой оценке ни один допущенный сэмпл не дал ни verdicts, ни critical_pass. С таким скорером фокус "
    "кампании строится только из сбоев исполнения (исход не completed); метрики фокус не строят."
)
FOCUS_UNAVAILABLE_RU = "проверка сигнала фокуса недоступна: вывод скорера не прочитан"

BASELINE_PROVENANCE_EN = ("Core has no stored provenance for any baseline sample: the target may not have reported it "
    "or may not have reported it on every turn. With gate.provenance.kind: reported, each attempt treats all cases "
    "as affected and causal checks run on all train.")

BASELINE_PROVENANCE_RU = (
    "Core не хранит provenance ни для одного сэмпла baseline: цель её не сообщила или сообщила не на всех ходах. "
    "При gate.provenance.kind: reported каждая попытка считает все кейсы затронутыми, "
    "и причинная проверка идёт по всему train."
)
BASELINE_PROVENANCE_UNAVAILABLE_RU = "Проверка provenance baseline недоступна: доказательства не прочитаны."


EVALUATION_PROVENANCE_NOTICES = {
    "baseline_provenance_unknown": (
        "The Target reported no provenance. A campaign with gate.provenance.kind: reported will "
        "treat every case as affected when using this evaluation as its baseline."
    ),
    "provenance_not_recorded": "This evaluation was made before provenance was recorded.",
}
