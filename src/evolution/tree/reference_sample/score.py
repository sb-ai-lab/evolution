"""Sample scorer: 1.0 when the target answered with the improved line.

Reads the one-object scorer document (`case_id`, `public`, `oracle`, `view`)
on stdin and writes `{"metrics": {"score": s}, "critical_pass": true,
"verdicts": [...]}` on stdout, `s` being 1.0 when the serialized trajectory
view contains `IMPROVED` and 0.0 otherwise. Every corpus row declares
`oracle.critical`, so `critical_pass` is required for exactly these cases.
The one verdict, `improved`, is the Reflector's failure channel: it passes
exactly when the score is 1.0, so the seed's samples carry a failed criterion
and the sample campaign has a failing cohort to reflect on. A sample, not a
recommended scorer.
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    payload = json.load(sys.stdin)
    score = 1.0 if "IMPROVED" in json.dumps(payload["view"]) else 0.0
    sys.stdout.write(
        json.dumps(
            {
                "metrics": {"score": score},
                "critical_pass": True,
                "verdicts": [{"criterion": "improved", "passed": score == 1.0}],
            }
        )
    )


if __name__ == "__main__":
    main()
