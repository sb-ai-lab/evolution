#!/usr/bin/env python3
"""Check the Lab distribution and public snapshot used by the assistant builders."""

from __future__ import annotations

import argparse
import json
import subprocess
import zipfile
from pathlib import Path

from check_release_artifacts import ROOT, check_archive

LAB = ROOT / "lab"
SELECTION = "scripts/public_snapshot/selection.json"


def check_checkout() -> set[str]:
    tracked = {
        path.decode()
        for path in subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"]).split(b"\0")
        if path
    }
    manifest = json.loads((ROOT / SELECTION).read_text(encoding="utf-8"))
    selected = manifest["paths"]
    if manifest["schema"] != 1 or manifest["projections"] or selected != sorted(tracked):
        raise ValueError("public selection manifest differs from the tracked snapshot")
    for path in sorted(tracked):
        if path.startswith(("lab/", "plugins/")) and path.endswith(".py"):
            compile((ROOT / path).read_bytes(), path, "exec")
    return {
        path.removeprefix("lab/src/")
        for path in tracked
        if path.startswith("lab/src/evolution_lab/")
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("sdist", type=Path)
    args = parser.parse_args()
    expected = check_checkout()
    for path in (args.wheel, args.sdist):
        unexpected = check_archive(path, source_root=LAB)
        if unexpected:
            raise ValueError(f"{path.name}: unexpected distribution members: {unexpected}")
    with zipfile.ZipFile(args.wheel) as archive:
        package_files = {name for name in archive.namelist() if name.startswith("evolution_lab/")}
    if package_files != expected:
        raise ValueError(
            f"missing Lab wheel members: {sorted(expected - package_files)}; "
            f"unexpected: {sorted(package_files - expected)}"
        )
    print(f"Public selection, Python sources and {len(expected)} Lab wheel members checked")


if __name__ == "__main__":
    main()
