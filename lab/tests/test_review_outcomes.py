import json
import pytest
from test_evaluation import bindings
from evolution_lab.evaluation import evaluate_fixed
from evolution_lab.report import read_evaluation_report, CostSummary

async def test_failed_evaluation_persists_reason_and_report(tmp_path):
    args = bindings(tmp_path, fail=True)
    result = await evaluate_fixed(**args)
    saved = args['run_directory'] / 'evaluation-result.json'
    assert saved.stat().st_mode & 0o777 == 0o600
    assert json.loads(saved.read_text())['stopped_reason'] == result['stopped_reason']
    report = read_evaluation_report(args['run_directory'])
    assert report.data['lab_result']['stopped_reason'] == result['stopped_reason']
    assert result['stopped_reason'] in report.markdown()

async def test_cancelled_task_records_closed_reason_without_exception_text(tmp_path):
    import asyncio
    args = bindings(tmp_path)
    def guard(): raise asyncio.CancelledError('PRIVATE-ERROR')
    with pytest.raises(asyncio.CancelledError):
        await evaluate_fixed(**args, dispatch_guard=guard)
    data = (args['run_directory'] / 'evaluation-result.json').read_text()
    assert json.loads(data)['stopped_reason'] == 'cancelled'
    assert 'PRIVATE-ERROR' not in data

async def test_report_labels_overrun_without_upper_bound_claim(tmp_path):
    args = bindings(tmp_path)
    await evaluate_fixed(**args)
    cost = CostSummary.from_budget({'limit_units': 10, 'reserved_upper_units': 20,
        'requests': 1, 'unknown_requests': 0, 'refusals': 1, 'overrun_requests': 1}, currency='RUB')
    report = read_evaluation_report(args['run_directory'], cost=cost)
    assert report.data['cost']['overrun_requests'] == 1
    assert 'верхняя оценка' not in report.markdown()

async def test_report_separates_bound_observations_from_an_accounting_stop(tmp_path):
    args = bindings(tmp_path)
    await evaluate_fixed(**args)
    cost = CostSummary.from_budget({'limit_units': 100, 'reserved_upper_units': 20,
        'requests': 3, 'unknown_requests': 0, 'refusals': 0, 'overrun_requests': 0,
        'bound_observations': 2}, currency='RUB')
    report = read_evaluation_report(args['run_directory'], cost=cost)
    assert report.data['cost']['bound_observations'] == 2
    markdown = report.markdown()
    assert 'верхняя оценка' in markdown  # the total is still bounded by the tariff
    assert 'выходов за объявленные границы: 2' in markdown
