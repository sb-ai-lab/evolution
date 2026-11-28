"""Regenerate the b15 old-path inventory from an exact Core CHANGELOG.

Usage: python tests/generate_core_moved_paths.py /path/to/CHANGELOG.md
The caller obtains that file from the audited development commit or final tag.
Both `old | new | Lab` and `old → new | Consumer` table layouts are accepted.
"""
import argparse
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("changelog", type=Path)
    parser.add_argument("--section", default="Unreleased",
                        help="CHANGELOG version heading; use 2.0.0b15 for the final tag")
    args = parser.parse_args()
    changelog = args.changelog.read_text()
    sections = re.split(r"^## ", changelog, flags=re.MULTILINE)[1:]
    section = next(part for part in sections if part.startswith(f"[{args.section}]"))
    pairs = []
    for line in section.splitlines():
        if line.startswith("|"):
            names = re.findall(r"`(evolution\.tree\.[\w.]+:\w+)`", line)
            if len(names) == 2:
                pairs.append(tuple(names))
    assert pairs, "No migration rows found in the newest CHANGELOG section"
    moved = sorted({old for old, new in pairs if old != new})
    destination = Path(__file__).parent / "data/core-b15-moved-paths.txt"
    destination.write_text("\n".join(moved) + "\n")
    print(f"{len(pairs)} table rows, {len(moved)} moved paths: {destination}")


if __name__ == "__main__":
    main()
