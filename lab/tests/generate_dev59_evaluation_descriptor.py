"""Regenerate the dev59 fixture from immutable master bytes, never during pytest.

Run with the exact b11 development environment Python from the repository root.
"""
from pathlib import Path
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile

BASELINE = "0d9672813b99ec26176a913f09cd61aa82bc6478"


def main():
    repository = Path(__file__).resolve().parents[1]
    archive = subprocess.check_output([
        "git", "archive", BASELINE, "src", "tests/test_preparation_evaluation.py",
        "tests/scaffold_15.py",
    ], cwd=repository)
    with tempfile.TemporaryDirectory(prefix="lab-dev59-descriptor-") as directory:
        root = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(root, filter="data")
        program = '''
import json
from pathlib import Path
from pytest import MonkeyPatch
from test_preparation_evaluation import _evaluation_bundle
from evolution_lab.preparation import PreparationStore
from evolution_lab.preparation.storage import canonical_json_bytes
with MonkeyPatch.context() as patch:
    bundle, state = _evaluation_bundle(Path("workspace"), patch)
    store = PreparationStore(state)
    prepared = store.prepare_evaluation(bundle.digest, selection={"count": 1})
    descriptor = store._storage.get_prepared(prepared.digest).descriptor
    payload = descriptor.to_payload()
    print(json.dumps({"producer_commit": "''' + BASELINE + '''",
                      "prepared_digest": prepared.digest,
                      "descriptor": payload,
                      "canonical_descriptor": canonical_json_bytes(payload).decode()}))
'''
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / "src"), str(root / "tests"))))
        output = subprocess.check_output([sys.executable, "-c", program], cwd=root, env=environment)
    value = json.loads(output)
    destination = repository / "tests/fixtures/preparation/dev59-evaluation-descriptor.json"
    destination.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(destination)
    print(value["prepared_digest"])


if __name__ == "__main__":
    main()
