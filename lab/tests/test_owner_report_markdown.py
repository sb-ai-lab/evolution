"""ReportMarkdownResult (spec 2026-10-14 report-markdown §2): the text of report.md, or a closed reason — never both."""
import pytest

from evolution_lab.owner import OwnerError, ReportMarkdownResult

pytestmark = pytest.mark.slow

OP = "a" * 32


def test_available_carries_the_text_and_no_reason():
    value = ReportMarkdownResult(OP, "available", "# Кампания\n", None)
    assert (value.operation_id, value.state, value.markdown, value.reason) == (OP, "available", "# Кампания\n", None)


@pytest.mark.parametrize("reason", ["results_deleted", "io_error", "unavailable"])
def test_unavailable_carries_a_reason_and_no_text(reason):
    assert ReportMarkdownResult(OP, "unavailable", None, reason).markdown is None


@pytest.mark.parametrize("args", [
    (OP, "available", None, None), (OP, "available", "x", "io_error"), (OP, "available", b"x", None),
    (OP, "unavailable", "x", "io_error"), (OP, "unavailable", None, None), (OP, "unavailable", None, "invalid"),
    (OP, "maybe", None, "io_error"), ("not an id", "available", "x", None),
])
def test_other_shapes_are_refused(args):
    with pytest.raises(OwnerError):
        ReportMarkdownResult(*args)


def test_report_markdown_takes_one_acquisition(tmp_path, monkeypatch):
    """Markdown uses the same campaign acquisition for status and report."""
    import asyncio
    from evolution_lab.owner import Owner, ResourceRef
    from evolution_lab.owner import readers
    from test_owner_cleanup import prepared_target, finished
    from test_owner_target import ready_target
    state, bundle, prepared = prepared_target(tmp_path)
    calls = []
    original = readers.acquire_campaign

    def counted(*args):
        calls.append(args)
        return original(*args)

    async def scenario():
        async with Owner.open(state) as owner:
            _, target, _ = await ready_target(owner, bundle)
            first = await owner.start(prepared.digest, target_ref=target, secrets={})
            ref = ResourceRef('operation', first.id)
            assert (await finished(owner, ref)).control_state == 'finished'
            monkeypatch.setattr(readers, 'acquire_campaign', counted)
            result = await owner.report_markdown(ref)
            assert len(calls) == 1 and result.state == 'available'
            assert result.markdown.startswith('# Кампания\n')
    asyncio.run(scenario())


@pytest.mark.parametrize(('results_state', 'reason'), [
    ('deleted', 'results_deleted'), ('deleting', 'io_error'), ('present', 'unavailable'),
])
def test_report_markdown_reasons_match_report(tmp_path, results_state, reason):
    """Deleted, deleting and unreadable journals keep the JSON report's reasons."""
    import asyncio
    from evolution_lab.owner import Owner
    from test_owner_delete import register, REF

    async def scenario():
        async with Owner.open(tmp_path / 'state') as owner:
            await register(owner)
            await owner._db('update_operation', REF.id, results_state=results_state)
            report = await owner.report(REF)
            markdown = await owner.report_markdown(REF)
            assert report.state == markdown.state == 'unavailable'
            assert report.reason == markdown.reason == reason
            assert markdown.markdown is None
    asyncio.run(scenario())


@pytest.mark.parametrize("scenario,expected", [
    ("focus-first", ["stages: focus rejected · causal not_run", "stages: focus passed · causal passed"]),
    ("focus-open", ["stages: focus open · causal waiting"]),
    ("focus-decided-open", ["stages: focus rejected · causal not_run"]),
    ("causal-open", ["stages: focus passed · causal open"]),
])
def test_no_economy_text_for_focus_first(scenario, expected):
    """Fresh real captures reach all three text clients once per attempt, without slot economy."""
    from io import StringIO
    from pathlib import Path
    from dev63_default_outputs import read_outputs
    from evolution_lab.cli.output import campaign_keyed_lines, write_value

    outputs = read_outputs(Path(__file__).parent / "fixtures/panel/b15" / scenario / "run", include_panel=False)
    view = outputs["campaign.json"]
    attempts = view["independent"]["attempts"]
    assert len(attempts) == len(expected)
    keyed = dict(campaign_keyed_lines(view))
    for attempt, line in zip(attempts, expected, strict=True):
        assert attempt["stages"]
        assert keyed[f"ind:{attempt['round']}:{attempt['attempt_id']}:stages"].strip() == line

    stream = StringIO()
    write_value(stream, outputs["owner-report.json"], machine=False)
    for text in (outputs["cli-campaign.txt"], stream.getvalue(), outputs["report.md"]):
        assert all(word not in text.lower() for word in (
            "not_owed", "not owed", "not dispatched", "slots saved", "saved"))
        assert [line.strip().removeprefix("- ") for line in text.splitlines() if "stages:" in line] == expected


@pytest.mark.parametrize("scenario,fixture", [
    ("agent-failed", "b14/agent-failed"),
    ("single-stage", "b15/insufficient-evidence"),
])
def test_default_text_unchanged(scenario, fixture):
    """Keep historical text apart from the intentional report readability changes."""
    from pathlib import Path
    from dev63_default_outputs import read_outputs
    from report_md_compat import without_operator_progress

    tests = Path(__file__).parent
    outputs = read_outputs(tests / "fixtures/panel" / fixture / "run", include_panel=False)
    baseline = tests / "data/dev63-default-outputs" / scenario
    original = "\n".join(line for line in without_operator_progress(outputs["report.md"]).split("\n")
                         if not line.startswith("без ответа: "))
    old = (baseline / "report.md").read_text(encoding="utf-8")
    digest = "fbd7fa5cd7695fbda3cd44d6052a66419e46395644eabfaf65a0dabc0dabc8c4"
    initial = f"Исходная версия: digest={digest}, measurement_id=m-r00-champion, version=v0"
    current = (f"Текущая версия: baseline_origin=seed, digest={digest}, measurement_complete=True, "
               "measurement_id=m-r00-champion, version=v0")
    assert initial in original.splitlines()
    assert current in original.splitlines()
    if scenario == "agent-failed":
        assert ('Окно неопределённости: {"counts": {"environment": 0, "rollout": 0, '
                '"scorer": 0, "target": 0}, "denominator": 0, "start": 17}') in original.splitlines()
    expected_lines = []
    for line in old.splitlines(keepends=True):
        if line.startswith("Исходная версия: "):
            line = initial + "\n"
        elif line.startswith("Текущая версия: "):
            line = current + "\n"
        elif line.startswith("Окно неопределённости: "):
            line = line.replace("&quot;", '"')
        line = line.replace("`lab release`", "`lab release-operation`")
        expected_lines.append(line)
    assert original.encode() == "".join(expected_lines).encode()
    header, separator, body = (baseline / "cli-campaign.txt").read_bytes().partition(b"\n")
    assert header.startswith(b"lab-campaign-view/13 ")
    expected = header.replace(b"lab-campaign-view/13", b"lab-campaign-view/15", 1) + separator + body
    assert outputs["cli-campaign.txt"].encode() == expected
