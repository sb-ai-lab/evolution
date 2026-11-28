"""Offline auto-resume with actual helper, worker, fake target and fake Editor."""
import asyncio
import hashlib
import json
from pathlib import Path

from scaffold_15 import scaffold_15_with_legacy_failures
from test_owner_target import make_bundle
import pytest

pytestmark = pytest.mark.slow


def _assert_absent(root, secrets):
    for path in root.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        contents = path.read_bytes()
        for secret in secrets:
            assert secret.encode() not in contents, f'sentinel found in {path.relative_to(root)}'


def test_offline_subprocess_auto_resume_keeps_secrets_out_of_state(tmp_path, caplog):
    from evolution_lab.execution_policy import policy_digest
    from evolution_lab.owner import Owner, ResourceRef, TargetRef
    from evolution_lab.preparation import PreparationStore

    target_secret = 'sentinel-target-private-7339528'
    role_secret = 'sentinel-editor-private-5281643'
    secrets = (target_secret, role_secret)
    flag = tmp_path / 'editor-fail'
    flag.write_text('fail', encoding='utf-8')
    role_receipt = tmp_path / 'role-checks.txt'
    target_receipt = tmp_path / 'target-check.txt'

    # Only the digest is embedded in the fixtures. The sentinel itself enters
    # the target and Editor exclusively through the Owner's secret API.
    def scaffold(path):
        sample = scaffold_15_with_legacy_failures(path)
        manifest = json.loads(sample.manifest.read_text())
        manifest['editor']['allowed_env'] = ['ROLE_SENTINEL']
        editor = sample.manifest.parent / manifest['editor']['cwd'] / manifest['editor']['argv'][1]
        editor.write_text('''import hashlib, json, os, sys
from pathlib import Path
value = os.environ.get("ROLE_SENTINEL", "")
if hashlib.sha256(value.encode()).hexdigest() != %(digest)r:
    raise SystemExit(42)
failing = Path(%(flag)r).exists()
with Path(%(receipt)r).open("a", encoding="utf-8") as log:
    log.write("failed-with-secret\\n" if failing else "resumed-with-secret\\n")
if failing:
    raise SystemExit(1)
print(json.dumps({"status": "declined", "note": "offline fake"}))
''' % {'digest': hashlib.sha256(role_secret.encode()).hexdigest(),
       'flag': str(flag), 'receipt': str(role_receipt)}, encoding='utf-8')
        sample.manifest.write_text(json.dumps(manifest), encoding='utf-8')
        return sample

    original_launcher = (Path(__file__).parent / 'owner_fixtures' / 'launcher.py').read_text()
    check_target = '''import hashlib
from pathlib import Path
value = os.environ.get("TARGET_TOKEN", "")
if hashlib.sha256(value.encode()).hexdigest() != %(digest)r:
    raise SystemExit(42)
Path(%(receipt)r).write_text("target-secret-checked\\n", encoding="utf-8")
''' % {'digest': hashlib.sha256(target_secret.encode()).hexdigest(), 'receipt': str(target_receipt)}
    launcher = original_launcher.replace('import os\n', 'import os\n' + check_target, 1)
    state, bundle, _ = make_bundle(tmp_path, max_instances=4, launcher_source=launcher,
                                   bundle_secret_names=('ROLE_SENTINEL',), scaffold=scaffold)
    prepared = PreparationStore(state).prepare_campaign(bundle.digest)
    policy = {'schema': 'lab-execution-policy/2', 'auto_resume': {
        'reasons': ['editor_failing'], 'max_resumes': 1, 'delay_seconds': 5,
        'busy_recheck_seconds': 1}}
    digest = policy_digest(policy)

    async def scenario():
        async with Owner.open(state) as owner:
            await owner.confirm_execution_policy(policy, expected_digest=digest, author='test', channel='api')
            connection = await owner.bind(bundle.digest)
            start_task = await owner.start_target(connection, secrets={'TARGET_TOKEN': target_secret},
                                                  execution_policy=digest)
            started = await asyncio.wait_for(owner.wait(start_task), 30)
            assert started.state == 'succeeded'
            target = TargetRef(started.ref.id, owner.owner_generation)
            accepted = await owner.start(prepared.digest, target_ref=target, secrets={'ROLE_SENTINEL': role_secret})
            ref = ResourceRef('operation', accepted.id)
            async with asyncio.timeout(120):
                while True:
                    current = await owner.status(ref)
                    if current.auto_resume['state'] == 'waiting':
                        break
                    assert current.auto_resume['state'] not in {'blocked', 'exhausted', 'closed'}
                    await asyncio.sleep(.05)
            assert current.result.core['suspensions'][-1]['reason'] == 'editor_failing'
            assert current.auto_resume['used'] == 0 and current.auto_resume['next_at']
            assert 'failed-with-secret' in role_receipt.read_text()
            assert target_receipt.read_text() == 'target-secret-checked\n'
            assert all(secret not in json.dumps(current.to_payload()) for secret in secrets)
            _assert_absent(state, secrets)
            flag.unlink()
            async with asyncio.timeout(120):
                while True:
                    current = await owner.status(ref)
                    if current.auto_resume['state'] == 'closed':
                        break
                    assert current.auto_resume['state'] not in {'blocked', 'exhausted', 'cancelled'}
                    await asyncio.sleep(.05)
            assert [(start.initiated_by, start.auto_resume_attempt) for start in current.starts] == [
                ('human', None), ('auto_resume', 1)]
            assert current.auto_resume['used'] == 1
            assert 'resumed-with-secret' in role_receipt.read_text()
            assert all(secret not in json.dumps(current.to_payload()) for secret in secrets)
            _assert_absent(state, secrets)
            await owner.close()
            assert owner._auto_resume.entries == {}
        _assert_absent(state, secrets)

    asyncio.run(scenario())
    assert all(secret not in caplog.text for secret in secrets)
