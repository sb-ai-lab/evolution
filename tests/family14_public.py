"""H12b's public family-14 driver: a genuine run that returns its public documents.

`tests.test_tree_campaign_family14_run.family14_run` stops every run before
the public report is assembled (`_Campaign.result` raises), because the
family-14 documents did not exist before H12b. This driver (H12b plan
choice 18) applies the same two seams -- the `/15` document rewrite and
`family14_admitted` -- and nothing else: `_Campaign.result` runs, so the
`CampaignResult` it returns carries the assembled, validated `result` and
`status` projections.

The rewrite resolves `family14_document` through the shared driver's module
at call time, so a test that patches it there (H12a's `edit_document`)
edits the documents of both drivers alike. The shared driver and the shared
fixtures are not edited.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evolution.tree.campaign import CampaignResult
from tests.campaign_drivers import OBSERVE, _run_kwargs
from tests.campaign_fakes import family14_admitted

#: The `/15` settings `family14_run` accepts (its `family14_settings`).
_SETTINGS = frozenset({"margin", "stability", "round_stop", "refresh", "grouper"})


async def family14_public_run(
    tmp_path: Path,
    monkeypatch: Any,
    config: dict[str, Any] = OBSERVE,
    *,
    family14_settings: dict[str, Any] | None = None,
    **kwargs: Any,
) -> tuple[Path, CampaignResult]:
    """Return `(run_root, CampaignResult)` of a genuine family-14 engine invocation.

    The arguments are `family14_run`'s: `config` is the family-13 fixture's
    gate/repeats/scorer/adapter settings, `family14_settings` may set
    `margin`, `stability`, `round_stop`, `refresh` and `grouper`, and other
    keywords go to `_two_case_run` (`terminal=None`; the caller asserts the
    terminal). Reusing the same path resumes the run.
    """

    from tests import campaign_drivers as fixtures
    from tests import test_tree_campaign_family14_run as driver

    family14_admitted(monkeypatch)
    real_evolve = fixtures.evolve

    async def evolve(path, *args, **options):
        document = json.loads(path.read_text())
        path.write_text(json.dumps(driver.family14_document(document, **family_options)))
        return await real_evolve(path, *args, **options)

    family_options, rest = _run_kwargs(config)
    if family14_settings is not None:
        if set(family14_settings) - _SETTINGS:
            raise ValueError(
                "driver settings support only margin, stability, round_stop, refresh and grouper"
            )
        family_options.update(family14_settings)
    if "adapter" in kwargs:
        rest.pop("used", None)
    kept: list[Any] = []
    # Scope the seam so a later call resumes without wrapping a wrapper.
    with monkeypatch.context() as patch:
        patch.setattr(fixtures, "evolve", evolve)
        await fixtures.two_case_run(tmp_path, terminal=None, keep=kept, **{**rest, **kwargs})
    (result,) = kept
    return tmp_path / "run", result
