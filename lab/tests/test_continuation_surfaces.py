"""New continuation surfaces coexist with the original closed service requests."""
import io
import json
from types import SimpleNamespace

import httpx
import pytest

pytestmark = pytest.mark.slow

D = 'a' * 64
TARGET = {'schema': 'lab-target-ref/1', 'id': 'target', 'owner_generation': 'generation'}


def test_v2_requests_and_unchanged_v1_shapes():
    from evolution_lab.service.codecs import decode_operation_request, decode_resume_request
    from evolution_lab.service.errors import ServiceError
    old_start = {'schema': 'lab-service-start-request/1', 'prepared_digest': D,
                 'target_ref': TARGET, 'secrets': {}}
    old_resume = {'schema': 'lab-service-resume-request/1', 'target_ref': TARGET, 'secrets': {}}
    assert decode_operation_request(old_start).prepared_digest == D
    assert decode_resume_request(old_resume).target_ref.id == 'target'
    with pytest.raises(ServiceError):
        decode_resume_request({**old_resume, 'reason': 'extra'})
    start = decode_operation_request({**old_start, 'schema': 'lab-service-start-request/2',
        'linked_from': 'previous', 'baseline': 'inherit', 'seed': 'champion'})
    assert (start.linked_from, start.baseline, start.seed) == ('previous', 'inherit', 'champion')
    resume = decode_resume_request({**old_resume, 'schema': 'lab-service-resume-request/2',
        'accept': [{'code': 'run_identity_changed', 'digest': D, 'effect': None}],
        'reason': 'repair', 'prepared_digest': D})
    assert resume.accept[0].digest == D and resume.reason == 'repair' and resume.prepared_digest == D


async def test_new_routes_pass_only_named_start_conditions():
    from evolution_lab.service.app import create_app
    from evolution_lab.owner import RefusalRecord, RefusalFinding
    finding = RefusalFinding('run_identity_changed', None, 'authorizable', D,
                              ('rebaseline',), ('fresh baseline',), ())
    class Runtime:
        service_environment = None
        def mutation_key(self, headers): return None
        async def call_mutation(self, *args, **kwargs):
            self.call = (args, kwargs)
            return {'schema': 'test/1'}
        async def call_owner(self, method, ref):
            assert method == 'refusal' and ref.id == 'operation'
            return RefusalRecord(ref.id, 2, (finding,))
    runtime = Runtime()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url='http://lab') as client:
        response = await client.get('/v1/operations/operation/refusal')
        assert response.status_code == 200
        assert response.json()['findings'][0]['effects'] == ['rebaseline']
        response = await client.post('/v1/operations/operation/resume', json={
            'schema': 'lab-service-resume-request/2', 'target_ref': TARGET, 'secrets': {},
            'accept': [{'code': 'run_identity_changed', 'digest': D, 'effect': None}],
            'reason': 'repair', 'prepared_digest': D})
        assert response.status_code == 202, response.text
        assert runtime.call[1]['accept'][0].digest == D
        assert runtime.call[1]['prepared'] == D


async def test_real_service_dispatch_admits_the_new_readers(tmp_path):
    from evolution_lab.service.app import create_app
    from evolution_lab.service.runtime import ServiceConfig, ServiceRuntime
    from test_service_runtime import config
    runtime = ServiceRuntime(config(tmp_path))
    await runtime.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url='http://lab') as client:
            for reader in ['refusal', 'inputs']:
                response = await client.get('/v1/operations/missing/' + reader)
                assert response.status_code == 404, response.text
                assert response.json()['error']['code'] == 'not_found'
    finally:
        await runtime.shutdown()


def test_service_cli_selects_v2_only_for_new_options():
    from test_service_cli import FakeClient, invoke, _status
    client = FakeClient([{'schema': 'lab-operation-refusal/1', 'operation_id': 'operation', 'start': 1,
        'findings': [{'code': 'run_identity_changed', 'path': 'policy', 'class': 'authorizable',
                     'instance_digest': D, 'effects': ['rebaseline'], 'effect_texts': ['fresh baseline'], 'differs': []}]},
        _status(None)])
    code, _, error = invoke(['--no-request-id', 'resume', 'operation', '--target', 'target',
        '--generation', 'generation', '--accept', 'run_identity_changed:' + D, '--reason', 'repair'], client)
    assert code == 0, error
    assert 'default means rebaseline' in error
    payload = client.calls[-1][2]
    assert payload['schema'] == 'lab-service-resume-request/2'
    assert payload['accept'] == [{'code': 'run_identity_changed', 'digest': D, 'effect': None}]
    assert payload['reason'] == 'repair' and payload['prepared_digest'] is None


def test_finding_readout_names_the_lab_acceptance():
    from evolution_lab.cli.output import finding_lines
    lines = finding_lines({'schema': 'lab-operation-refusal/1', 'operation_id': 'op', 'start': 1,
        'findings': [{'code': 'run_identity_changed', 'path': 'policy', 'class': 'authorizable',
            'instance_digest': D, 'effects': ['rebaseline', 'close'],
            'effect_texts': ['fresh baseline', 'closes the campaign'], 'differs': ['policy changed']}]},
        command=['lab', 'resume', 'op', '--target', 'target', '--generation', 'generation'])
    text = '\n'.join(lines)
    assert ' at policy' in text and 'instance_digest: ' + D in text
    assert '--accept run_identity_changed:' + D in text
    assert '--effect rebaseline' in text and 'closes the campaign' in text


def test_missing_reason_is_refused_before_project_or_service_work(tmp_path):
    import asyncio
    from evolution_lab.quickstart._campaign import campaign
    from evolution_lab.quickstart import QuickstartError
    from evolution_lab.owner import Accepted
    from test_service_cli import FakeClient, invoke
    with pytest.raises(QuickstartError) as error:
        asyncio.run(campaign(tmp_path, wheels=(), continue_run=True,
                             accept=[Accepted('run_identity_changed', D)]))
    assert error.value.code == 'authorization_reason_required'
    client = FakeClient()
    code, _, text = invoke(['resume', 'operation', '--target', 'target', '--generation', 'generation',
                            '--accept', 'run_identity_changed:' + D], client)
    assert code == 2 and '--reason' in text and client.calls == []


@pytest.mark.parametrize('options,expected', [(['--continue'], 'continue_run'), (['--from', 'previous'], 'linked_from')])
def test_project_cli_passes_the_followup(tmp_path, monkeypatch, options, expected):
    from evolution_lab.cli import main
    import evolution_lab.quickstart._campaign as api
    (tmp_path / 'campaign-launch.json').write_text('{}')
    seen = {}
    async def campaign(project, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(exit_code=0, summary={'done': True}, run_dir=tmp_path)
    monkeypatch.setattr(api, 'campaign', campaign)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(['campaign', str(tmp_path), '--wheel', 'lab.whl', '--wheel', 'core.whl', '--json', *options],
                stdout=stdout, stderr=stderr)
    assert code == 0, stderr.getvalue()
    assert seen[expected] == (True if expected == 'continue_run' else 'previous')
    assert json.loads(stdout.getvalue()) == {'done': True}


def test_project_continuation_reuses_the_same_operation(tmp_path):
    from evolution_lab.projects.demo import campaign_project
    from test_quickstart_campaign import _campaign
    project = campaign_project(tmp_path / 'project')
    flag = tmp_path / 'unavailable'; flag.write_text('')
    agent = project / 'agent/my_agent.py'
    source = agent.read_text().replace('    def answer(self, question):',
        '    def answer(self, question):\n'
        f'        from pathlib import Path\n        if Path({str(flag)!r}).exists():\n'
        '            raise OSError("temporary test fault")')
    agent.write_text(source)
    manifest = project / 'manifest.json'
    raw = json.loads(manifest.read_text())
    raw['campaign']['sample_retries'] = 0
    raw['campaign']['budget']['max_failed_turns'] = 1
    for limits in raw['campaign']['indeterminate'].values():
        limits.update(max_count=1, max_rate='1')
    manifest.write_text(json.dumps(raw))
    first = _campaign(project)
    assert first.summary['campaign']['suspended'] is True, first.summary
    assert first.exit_code == 4
    flag.unlink()
    second = _campaign(project, continue_run=True)
    assert second.exit_code == 0, second.summary
    assert first.summary['campaign']['operation_id'] == second.summary['campaign']['operation_id']
    history = json.loads((second.run_dir / 'inputs.json').read_text())
    assert len(history['starts']) == 2
    assert history['starts'][0]['prepared_digest'] == history['starts'][1]['prepared_digest']


@pytest.mark.parametrize('seed', ['champion', 'project'])
def test_project_linked_start_uses_the_promoted_champion(tmp_path, seed):
    from test_owner_linked_successor import _promoted_project
    from test_quickstart_campaign import _campaign
    first = _promoted_project(tmp_path)
    second = _campaign(tmp_path / 'project', linked_from=first.summary['campaign']['operation_id'], seed=seed)
    assert second.exit_code in {0, 1}, second.summary
    assert second.summary['campaign']['failure'] is None
    from evolution_lab.owner import Owner, ResourceRef
    import asyncio
    async def read():
        async with Owner.open(second.summary['state']) as owner:
            ref = ResourceRef('operation', second.summary['campaign']['operation_id'])
            core = (await owner.status(ref)).result.core
            if seed == 'champion':
                assert core['linked_from']['baseline']['imported'] > 0
            else:
                from evolution_lab.quickstart._readout import render_campaign_readout
                assert 'Core caveat: condition_changed' in render_campaign_readout(second.summary, second.run_dir)
    asyncio.run(read())


@pytest.mark.parametrize("version", (1, 2, 3))
async def test_start_request_routes_preserve_existing_shapes(tmp_path, version):
    from evolution_lab.owner import BaselineImportChoice
    from evolution_lab.service.app import create_app

    class Runtime:
        config = SimpleNamespace(state_directory=tmp_path)
        service_environment = None

        def mutation_key(self, headers):
            return None

        async def call_mutation(self, *args, **kwargs):
            self.call = args, kwargs
            return {"schema": "test/1"}

    body = {"schema": f"lab-service-start-request/{version}", "prepared_digest": D,
            "target_ref": TARGET, "secrets": {}}
    if version >= 2:
        body.update(linked_from="previous" if version == 2 else None, baseline="inherit", seed="champion")
    if version == 3:
        body.update(baseline_from="evaluation")
    runtime = Runtime()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)),
                                 base_url="http://lab") as client:
        response = await client.post("/v1/operations", json=body)
    assert response.status_code == 202, response.text
    args, kwargs = runtime.call
    assert args[2] == {"body": body}
    assert args[3:] == ("owner", "start", D)
    if version < 3:
        assert "baseline_import" not in kwargs
    else:
        assert kwargs["baseline_import"] == BaselineImportChoice.for_operation(tmp_path, "evaluation")
    if version == 1:
        assert set(kwargs) == {"target_ref", "secrets"}
    else:
        assert kwargs["linked_from"] == body["linked_from"]
        assert (kwargs["baseline"], kwargs["seed"]) == ("inherit", "champion")


@pytest.mark.parametrize("options,version", [
    ([], 1), (["--linked-from", "previous"], 2), (["--baseline-from", "evaluation"], 3),
])
def test_service_cli_start_selects_the_request_version(options, version):
    from test_service_cli import FakeClient, invoke, _status

    client = FakeClient([_status(None)])
    code, _, error = invoke(["--no-request-id", "start", D, "--target", "target",
                             "--generation", "generation", *options], client)
    assert code == 0, error
    body = client.calls[-1][2]
    expected = {"schema": f"lab-service-start-request/{version}", "prepared_digest": D,
                "target_ref": TARGET, "secrets": {}}
    if version >= 2:
        expected.update(linked_from="previous" if version == 2 else None, baseline="inherit", seed="champion")
    if version == 3:
        expected["baseline_from"] = "evaluation"
    assert body == expected


@pytest.mark.parametrize("extra", [["--baseline", "fresh"], ["--baseline", "inherit"], ["--seed", "project"],
                                   ["--seed", "champion"]])
def test_service_cli_refuses_link_controls_with_baseline_from(extra):
    """--baseline and --seed shape a linked start; with --baseline-from they would be ignored."""
    from test_service_cli import FakeClient, invoke

    client = FakeClient([])
    code, _, error = invoke(["--no-request-id", "start", D, "--target", "target", "--generation", "generation",
                             "--baseline-from", "evaluation", *extra], client)
    assert code == 2 and "--baseline-from" in error
    assert client.calls == []


def test_service_schemas_unchanged():
    """Pin the pre-import request and response bytes across the CLI and service."""
    import asyncio

    from evolution_lab.cli.client import ServiceClient
    from evolution_lab.owner import Accepted, RefusalFinding, RefusalRecord, ResourceRef, TargetRef
    from evolution_lab.owner.models import (
        CleanupFacts,
        ExecutorIdentity,
        OperationStatus,
        ProcessFacts,
    )
    from evolution_lab.service.app import create_app
    from test_service_cli import invoke

    digest = D.encode()
    target_bytes = b'{"schema":"lab-target-ref/1","id":"target","owner_generation":"generation"}'
    start_bytes = b'{"schema":"lab-service-start-request/%d","prepared_digest":"%s","target_ref":%s,"secrets":{}'
    resume_bytes = b'{"schema":"lab-service-resume-request/%d","target_ref":%s,"secrets":{}'
    status_bytes = (
        b'{"schema":"lab-operation-status/8","id":"operation","owner_generation":"generation",'
        b'"prepared_digest":"%s","kind":"campaign","target_ref":%s,"control_state":"finished",'
        b'"process":{"launched":true,"exit_code":1,"cleanup_complete":true,"forced":false},'
        b'"target_cleanup":{"state":"confirmed","complete":true},"result":null,"failure":null,'
        b'"refusal":null,"core_terminal":null,"results_state":"present",'
        b'"executor":{"kind":"lab-campaign-worker/1","environment_digest":"%s","lab_wheel_sha256":"%s",'
        b'"core_wheel_sha256":"%s","module_hashes":{}},"retained_summary":null,"starts":[],"resume":null,'
        b'"entrypoint":null,"progress":null,"progress_state":"not_started","times":null,'
        b'"times_state":"not_started","times_source":null,"times_reason":null,'
        b'"auto_resume":{"policy_digest":null,"state":"off","next_at":null,"used":0,'
        b'"max":null,"blocked":null,"detail":null}}'
    ) % (digest, target_bytes, digest, digest, digest)
    refusal_bytes = (
        b'{"schema":"lab-operation-refusal/1","operation_id":"operation","start":1,'
        b'"findings":[{"code":"run_identity_changed","path":"policy","class":"authorizable",'
        b'"instance_digest":"%s","effects":["rebaseline"],"effect_texts":["fresh baseline"],'
        b'"differs":["policy changed"]}]}'
    ) % digest
    op = ResourceRef("operation", "operation")
    target = TargetRef("target", "generation")
    status = OperationStatus(
        op.id,
        "generation",
        D,
        target,
        "finished",
        ProcessFacts(True, 1, True, False),
        CleanupFacts("confirmed", True),
        None,
        None,
        "present",
        ExecutorIdentity(D, D, D, {}),
    )
    finding = RefusalFinding(
        "run_identity_changed",
        "policy",
        "authorizable",
        D,
        ("rebaseline",),
        ("fresh baseline",),
        ("policy changed",),
    )
    refusal = RefusalRecord(op.id, 1, (finding,))
    owner_calls = []

    class Runtime:
        service_environment = None

        def mutation_key(self, headers):
            return None

        async def call_mutation(self, key, method, request, scope, member, value, **kwargs):
            assert scope == "owner"
            owner_calls.append((member, value, kwargs))
            return status

        async def call_owner(self, method, ref):
            assert ref == op
            return {"status": status, "refusal": refusal}[method]

    app = create_app(Runtime())
    received = []

    async def forward(request):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://lab"
        ) as service:
            return await service.request(
                request.method, request.url, headers=request.headers, content=request.content
            )

    def exchange(request):
        response = asyncio.run(forward(request))
        received.append(
            (
                request.method,
                request.url.path,
                request.content,
                response.status_code,
                response.content,
            )
        )
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    target_args = ["--target", target.id, "--generation", target.owner_generation]
    for argv in (
        ["start", D, *target_args],
        ["start", D, *target_args, "--linked-from", "previous"],
        ["resume", op.id, *target_args],
        [
            "resume",
            op.id,
            *target_args,
            "--accept",
            f"run_identity_changed:{D}",
            "--reason",
            "repair",
        ],
        ["status", op.id],
    ):
        with ServiceClient(
            socket_path="/tmp/lab.sock", transport=httpx.MockTransport(exchange)
        ) as client:
            options = [] if argv[0] == "status" else ["--no-request-id"]
            code, _, error = invoke([*options, *argv], client)
        assert code == 0, (argv, error)

    root = "/v1/operations"
    resume = f"{root}/{op.id}/resume"
    assert received == [
        ("POST", root, start_bytes % (1, digest, target_bytes) + b"}", 202, status_bytes),
        (
            "POST",
            root,
            start_bytes % (2, digest, target_bytes)
            + b',"linked_from":"previous","baseline":"inherit","seed":"champion"}',
            202,
            status_bytes,
        ),
        ("POST", resume, resume_bytes % (1, target_bytes) + b"}", 202, status_bytes),
        ("GET", f"{root}/{op.id}/refusal", b"", 200, refusal_bytes),
        (
            "POST",
            resume,
            resume_bytes
            % (2, target_bytes)
            + b',"accept":[{"code":"run_identity_changed","digest":"%s","effect":null}],'
            b'"reason":"repair","prepared_digest":null}' % digest,
            202,
            status_bytes,
        ),
        ("GET", f"{root}/{op.id}", b"", 200, status_bytes),
    ]
    common = {"target_ref": target, "secrets": {}}
    assert owner_calls == [
        ("start", D, common),
        (
            "start",
            D,
            {**common, "linked_from": "previous", "baseline": "inherit", "seed": "champion"},
        ),
        ("resume", op, common),
        (
            "resume",
            op,
            {
                **common,
                "accept": (Accepted("run_identity_changed", D, None),),
                "reason": "repair",
                "prepared": None,
            },
        ),
    ]
