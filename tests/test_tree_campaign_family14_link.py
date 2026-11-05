"""H12a: the link family rule (R70), pure."""

from evolution.tree.campaign import _linkable_families


def test_family_14_links_only_family_14():
    # ADR-0032's 2026-10-20 amendment excludes family 13 from new links.
    assert _linkable_families(14) == (14,)
