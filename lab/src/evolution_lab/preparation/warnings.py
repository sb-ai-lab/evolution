"""Non-refusing preparation warnings (ADR-0013, decision 6)."""
from __future__ import annotations

import json

from evolution_lab.preparation.models import BundleWarning, EnvironmentDescriptor, StoredBundle


def _lab_pin(descriptor: EnvironmentDescriptor) -> str | None:
    return next((pin.version for pin in descriptor.distributions if pin.name == "evolution-lab"), None)


def bundle_warnings(stored: StoredBundle) -> tuple[BundleWarning, ...]:
    """Compute non-refusing warnings for an already-composed Bundle.

    Read-only: never mutates the descriptor and never affects the Bundle digest.
    """
    launch = json.loads((stored.path / stored.descriptor.launch).read_bytes())
    if launch.get("schema") not in ("lab-target-launch/2", "lab-target-launch/3"):
        return ()
    environments = {environment.digest: environment for environment in stored.descriptor.environments}
    expected = _lab_pin(stored.descriptor.environments[0])
    found = _lab_pin(environments[launch["environment_ref"]])
    if found == expected:
        return ()
    return (BundleWarning("lab_version_mismatch", {"expected": expected, "found": found}),)
