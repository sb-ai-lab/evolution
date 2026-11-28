"""Family14 role suspensions carry a hint (core a21, spec 2026-09-30 §2.4)."""
import json
from pathlib import Path

import pytest

from evolution_lab._suspension import ROLE_SUSPENSIONS, suspension_hint


def test_every_family14_role_reason_has_a_hint():
    assert set(ROLE_SUSPENSIONS) == {"reflector_provider_failed", "reflector_failing",
                                     "editor_provider_failed", "editor_failing"}
    assert suspension_hint("target_unstable", "en") is None
    assert suspension_hint("target_unstable", "ru") is None


def test_hint_language_is_selected_by_lang():
    for reason, (english, russian) in ROLE_SUSPENSIONS.items():
        assert suspension_hint(reason, "en") == english
        assert suspension_hint(reason, "ru") == russian
        # The hint is the repair phrase only: how to continue depends on the path (Owner, legacy, H1)
        # and the report/CLI continuation line says it.
        assert "lab resume" not in english and "lab resume" not in russian
        assert "`" not in english and "`" not in russian


def test_failing_hints_name_transient_causes_and_say_that_bundle_content_needs_a_new_campaign():
    for reason in ("reflector_failing", "editor_failing"):
        english, russian = ROLE_SUSPENSIONS[reason]
        assert "timeout" in english and "new Bundle" in english
        assert "таймаут" in russian and "новый Bundle" in russian
        if reason == "editor_failing":
            assert "environment" in english and "окружени" in russian
        assert "turn budget" not in english and "бюджет ходов" not in russian
    for reason in ("reflector_provider_failed", "editor_provider_failed"):
        english, russian = ROLE_SUSPENSIONS[reason]
        assert "availability, key and quota" in english and "endpoint" not in english
        assert "endpoint" not in russian


def test_failing_hints_follow_the_core_trigger_model_and_do_not_send_the_user_to_the_environment():
    # `reflector_failing` also fires on a second skipped group in a row; `editor_failing` never does
    # (an Editor content failure closes the Attempt). The interpreter and installed tools are part of
    # the Bundle's captured environment: Lab refuses a change of it, so they are not a repair to offer.
    reflector_en, reflector_ru = ROLE_SUSPENSIONS["reflector_failing"]
    editor_en, editor_ru = ROLE_SUSPENSIONS["editor_failing"]
    assert "skipped" in reflector_en and "пропущен" in reflector_ru
    assert "skip" not in editor_en and "пропущ" not in editor_ru
    for english, russian in ROLE_SUSPENSIONS.values():
        assert "interpreter" not in english and "installed tools" not in english
        assert "интерпретатор" not in russian and "установленные инструменты" not in russian


def test_reflector_failing_hint_splits_the_remedy_by_trigger():
    # An external or transient failure is repaired and continued; repeated content skips are continued only
    # if the failures were transient, otherwise the Reflector's inputs change (new Bundle, new campaign).
    english, russian = ROLE_SUSPENSIONS["reflector_failing"]
    assert "transient" in english and "max_consecutive_failures" in english
    assert "fix the external cause" not in english
    assert "временн" in russian and "max_consecutive_failures" in russian
    assert "устраните внешнюю причину и продолжайте" not in russian


def test_champion_incomplete_hint_names_the_same_action_and_core_remeasuring():
    """Spec 2026-10-13 §2.3: core re-measures each listed Champion pair once on the next plain resume."""
    from evolution_lab._suspension import CHAMPION_SUSPENSIONS
    from evolution_lab.owner import core_raw
    assert set(CHAMPION_SUSPENSIONS) == core_raw.champion_suspension_reasons()
    assert suspension_hint("champion_incomplete", "ru") == (
        "Замер Champion не завершён: остановка оборвала его или совпала с другой приостановкой. Продолжите "
        "кампанию тем же действием на готовой цели: core один раз перемерит записанные core пары Champion.")
    assert suspension_hint("champion_incomplete", "en") == (
        "The Champion's measurement was left unfinished: a stop cut it short or came with another suspension. "
        "Continue the campaign with the same action on a ready target: core re-measures each Champion pair core recorded "
        "once.")


def test_family_14_suspensions_are_core_s_and_have_hints():
    """Spec 2026-10-13 §6.4: the pauses only family 14 reaches; connection failures name the Bundle boundary."""
    from evolution_lab._suspension import FINAL_SUSPENSIONS
    from evolution_lab.owner import core_raw
    from evolution.tree.campaign_schema import CONNECTION_SUSPENSIONS, GROUPER_SUSPENSIONS
    assert set(FINAL_SUSPENSIONS) == set(CONNECTION_SUSPENSIONS) | set(GROUPER_SUSPENSIONS) | {"final_incomplete"}
    assert set(FINAL_SUSPENSIONS) <= core_raw.suspension_reasons()
    assert suspension_hint("final_incomplete", "en") == (
        "The round's final measurement was left unmeasured by an infrastructure failure. Continue the campaign with "
        "the same action on a ready target: core re-measures the missing final pairs.")
    assert suspension_hint("final_incomplete", "ru") == (
        "Финальный замер раунда не домерен из-за инфраструктуры. Продолжите кампанию тем же действием на готовой "
        "цели: core перемерит недостающие пары финала.")
    assert suspension_hint("connection_incompatible", "en").endswith("fix the package and start a new campaign.")
    assert suspension_hint("connection_preflight_unavailable", "ru").startswith("Цель или скорер были недоступны")
    assert "budget" in suspension_hint("connection_preflight_inconclusive", "en")
    assert suspension_hint("grouper_provider_failed", "en").startswith("the Grouper's provider failed")
    assert "new Bundle" in suspension_hint("grouper_failing", "en")


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_description_secret_hint_points_to_describe(lang):
    hint = suspension_hint("connection_incompatible", lang, "connection_description_secret")
    assert "describe()" in hint and "metric_contract" not in hint
    if lang == "en":
        assert "new package" in hint and "new campaign" in hint
    else:
        assert "новый пакет" in hint and "новую кампанию" in hint
    # Unknown or absent details retain the existing metric-contract advice.
    assert "metric_contract" in suspension_hint("connection_incompatible", lang)
    assert "metric_contract" in suspension_hint("connection_incompatible", lang, "connection_field_empty")


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_record_matches_secret_hint_says_new_campaign(lang):
    hint = suspension_hint("persistence_failed", lang, "record_matches_secret")
    assert "describe()" in hint
    if lang == "en":
        assert "cannot continue this campaign in Lab" in hint
        assert "new package" in hint and "new campaign" in hint
    else:
        assert "в Lab эту кампанию продолжить нельзя" in hint
        assert "новый пакет" in hint and "новую кампанию" in hint
    assert suspension_hint("persistence_failed", lang) is None
    assert suspension_hint("persistence_failed", lang, "journal_unavailable") is None


@pytest.mark.parametrize("surface", ["campaign", "status", "markdown"])
def test_description_secret_hint_reaches_existing_consumers(surface):
    from evolution_lab.cli.output import campaign_keyed_lines, suspension_lines
    from evolution_lab.report import Report, build_report
    from test_report import campaign

    # Existing Core projection shape; synthetic detail only, no journal or DTO changes.
    core = json.loads((Path(__file__).parent / "fixtures/family14/connection-suspended-status.json").read_text())
    core["connection"]["detail"]["code"] = "connection_description_secret"
    if surface == "campaign":
        view = {"schema": "lab-campaign-view/15", "family": 14, "connection": core["connection"],
                "progress": {"phase": "suspended"}, "suspensions": core["suspensions"]}
        text = "\n".join(line for key, line in campaign_keyed_lines(view) if key == "suspended-hint")
    elif surface == "status":
        text = "\n".join(line for line in suspension_lines({"result": {"core": core}})
                         if line.startswith("hint:"))
    else:
        data = build_report(campaign()).data
        data["core"].update(suspended=True, suspensions=core["suspensions"], connection=core["connection"])
        text = Report(json.dumps(data)).markdown()
    assert "describe()" in text and "metric_contract" not in text


@pytest.mark.parametrize("terminal", ["storage_capacity_exhausted", "persistence_failed", "future_terminal"])
def test_projection_invalid_hint_preserves_the_journal(terminal):
    english = suspension_hint(terminal, "en", "projection_invalid")
    russian = suspension_hint(terminal, "ru", "projection_invalid")
    assert english == ("Core's report could not be projected; the journal is intact. "
                       "Install a fixed Core and read the report again.")
    assert russian == ("Не удалось построить отчёт Core; журнал сохранён. "
                       "Установите исправленный Core и прочитайте отчёт снова.")


@pytest.mark.parametrize("lang", ["en", "ru"])
@pytest.mark.parametrize("terminal", ["finished", "suspended"])
def test_projection_invalid_hint_names_the_safe_action_for_the_terminal(terminal, lang):
    hint = suspension_hint(terminal, lang, "projection_invalid")
    if lang == "en":
        assert "report could not be projected" in hint and "journal is intact" in hint
        assert "Install a fixed Core" in hint
    else:
        assert "Не удалось построить отчёт Core" in hint and "журнал сохранён" in hint
        assert "Установите исправленный Core" in hint
    if terminal == "finished":
        assert "evo tree watch --once" in hint and "evolve" not in hint
    else:
        assert "evo tree evolve" in hint and "watch" not in hint
        assert ("same" if lang == "en" else "тем же") in hint
        assert ("after one rebase" if lang == "en" else "после одного rebase") in hint
