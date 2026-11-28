"""Project and implementation identifiers (spec §3.2)."""
from __future__ import annotations

import random
import re
from typing import Callable

from .errors import ProjectError

_ID = re.compile(r"[a-z][a-z0-9-]{1,39}\Z")
ADJECTIVES = (
    "amber", "brave", "calm", "clever", "crisp", "deep", "eager", "fair", "fast", "gentle",
    "glad", "golden", "grand", "green", "happy", "jolly", "keen", "kind", "light", "lively",
    "lucky", "mellow", "merry", "mild", "misty", "noble", "patient", "plain", "proud", "quick",
    "quiet", "rapid", "ready", "silent", "silver", "simple", "smart", "snowy", "solid", "steady",
    "still", "sunny", "swift", "tidy", "vivid", "warm", "wise", "witty",
)
NOUNS = (
    "anchor", "aspen", "badger", "beacon", "birch", "brook", "canyon", "cedar", "comet", "coral",
    "crane", "delta", "dune", "ember", "falcon", "fern", "field", "fjord", "forest", "galaxy",
    "glade", "harbor", "heron", "hill", "island", "lagoon", "lake", "maple", "meadow", "mesa",
    "moon", "oak", "otter", "owl", "pine", "planet", "prairie", "quartz", "river", "robin",
    "sparrow", "spruce", "star", "stone", "summit", "tide", "valley", "willow",
)


def validate_id(value: object) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ProjectError("invalid_id")
    return value


def generate_id(taken: Callable[[str], bool], *, rng: random.Random | None = None) -> str:
    """W&B-style ``adjective-noun-NN``; unique against ``taken`` (archive included)."""
    rng = rng or random.SystemRandom()
    for _ in range(200):
        value = f"{rng.choice(ADJECTIVES)}-{rng.choice(NOUNS)}-{rng.randrange(100):02d}"
        if not taken(value):
            return value
    raise ProjectError("id_taken")
