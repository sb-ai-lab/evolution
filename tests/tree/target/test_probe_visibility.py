"""Probe citations through real campaign admission and role projections."""

import pytest

from evolution.tree.roles.reflector_contracts import GroupError, validate_groups
from tests.test_tree_adaptive_reflector import group

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("refs", [[True], [1.0], ["1"], [0], [-1], [2], None, (1,)])
def test_probe_refs_require_current_exact_integer_sequences(refs):
    with pytest.raises(GroupError, match="probe_refs") as error:
        validate_groups(
            [group(probe_refs=refs)],
            catalog_ids={"obs:a:1": "a"},
            observation_ids={"obs:a:1": "a"},
            case_ids={"a"},
            editable_paths=("*.md",),
            probe_seqs={1},
            needles=(),
        )
    assert error.value.code == "invalid_result"
