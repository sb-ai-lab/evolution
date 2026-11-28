"""V2: source answers stay outside target input; modified inputs are explicit."""
from observations_support import make_import


def test_basket_target_input_and_recorded_plan(tmp_path):
    from evolution_lab.observations import Selection, build_basket, prepare_recorded_evaluation
    observations = make_import(tmp_path)
    execution = observations.executions[0]
    basket = build_basket([observations], [Selection(execution['ref'], group_ref='group-1')], tmp_path/'basket')
    case = basket.cases[0]
    assert case['input'] == 'Sort: 3, 1, 2'
    assert case['context'] is None
    assert 'output' not in case
    assert '1, 2, 3' not in str(case)
    plan = prepare_recorded_evaluation(basket, [observations], tmp_path/'plan')
    row = plan.data['cases'][0]
    assert row['record_available'] is True
    assert row['parts']['context'] == 'unknown'
    assert row['identity_status'] == 'known'
    assert row['input_modified'] is False
    assert 'score' not in row and 'repeats' not in row


def test_modified_question_not_represented_as_original_execution(tmp_path):
    from evolution_lab.observations import Selection, build_basket, prepare_recorded_evaluation
    obs = make_import(tmp_path)
    basket = build_basket([obs], [Selection(obs.executions[0]['ref'], replacements={'input':'Sort: 5, 4'})], tmp_path/'basket')
    row = prepare_recorded_evaluation(basket,[obs],tmp_path/'plan').data['cases'][0]
    assert row['input_modified'] is True
    assert basket.cases[0]['input'] == 'Sort: 5, 4'


def test_reimported_source_is_not_an_extra_repeat(tmp_path):
    from evolution_lab.observations import Selection, build_basket, prepare_recorded_evaluation
    first_path = tmp_path/'first'; first_path.mkdir()
    second_path = tmp_path/'second'; second_path.mkdir()
    a,b = make_import(first_path), make_import(second_path)
    basket = build_basket([a,b], [Selection(a.executions[0]['ref']),Selection(b.executions[0]['ref'])],tmp_path/'basket')
    rows = prepare_recorded_evaluation(basket,[a,b],tmp_path/'plan').data['cases']
    assert rows[0]['possible_overlap_with'] == [{'case_ref':rows[1]['case_ref'],'basis':'same_stable_key'}]
    assert all('repeats' not in r for r in rows)
