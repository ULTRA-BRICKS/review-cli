"""Exercise run discovery and attachment through the CLI with isolated providers."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    shim = tmp_path / 'shim'
    shim.mkdir()
    (shim / 'sitecustomize.py').write_text('''
import os
from reviewlib import backends

def fake(backend, model, prompt, diff, cwd, timeout, round_no=None, **kwargs):
    failed = os.environ.get("TEST_PROVIDER_FAIL") == "1"
    from reviewlib.process import write_sidecar_log
    write_sidecar_log(backend="fake", round_no=round_no or 0, argv0="fake:" + model,
                     returncode=1 if failed else 0, stdout="test-provider evidence", stderr="")
    return backends.ReviewResult(model, "test-provider", 1 if failed else 0,
        "" if failed else "Evidence: input reviewed. No findings.", "provider failed" if failed else "")
backends.call_backend = fake
from reviewlib import panel
panel.call_backend = fake
if os.environ.get("TEST_VISION"):
    from reviewlib.features.visual import vision_client as vision
    vision.vision_backend_available = lambda model: True
    def fake_vision(model, system, blocks, schema, timeout_s, effort=None):
        assert any(block.kind == "image" and block.data_base64 for block in blocks)
        from pathlib import Path
        Path(os.environ["TEST_VISION"]).write_text("received image")
        return vision.VisionVerdict(available=True, verdict="keep", confidence=1.0, backend=model)
    vision._call_codex_cli = fake_vision
''')
    env = {'HOME': str(home), 'PATH': os.environ['PATH'],
           'PYTHONPATH': os.pathsep.join([str(shim), str(ROOT)]),
           'REVIEW_FAKE_BACKEND': '1',
           'REVIEW_JOBS_DIR': str(home / 'jobs'),
           'REVIEW_STATS_FILE': str(home / 'stats.jsonl')}
    return env


def cli(env, *args):
    return subprocess.run([sys.executable, str(ROOT / 'bin/review'), *args],
                          env=env, cwd=ROOT, input='', text=True,
                          capture_output=True, timeout=30)


def test_unassociated_run_attach_preserves_evidence(isolated):
    run = cli(isolated, 'just-ask', 'What changed?', '-m', 'codex', '--pool', '1')
    assert run.returncode == 0, run.stderr
    records = json.loads(cli(isolated, 'jobs', '--json').stdout)
    assert len(records) == 1
    run_id = records[0]['job_id']
    assert f'review attach {run_id} --task CODE' in run.stderr
    before = Path(isolated['REVIEW_STATS_FILE']).read_bytes()
    attach = cli(isolated, 'attach', run_id, '--task', 'TEST-1')
    assert attach.returncode == 0, attach.stderr
    assert cli(isolated, 'attach', run_id, '--task', 'TEST-1').returncode == 0
    assert cli(isolated, 'attach', run_id, '--task', 'TEST-2').returncode != 0
    assert Path(isolated['REVIEW_STATS_FILE']).read_bytes() == before
    evidence = cli(isolated, 'task', 'TEST-1', '--json')
    assert evidence.returncode == 0, evidence.stderr
    assert run_id in evidence.stdout


@pytest.mark.parametrize('code', ['', 'two words', 'x' * 121, 'bad\x01code'])
def test_invalid_supplied_task(isolated, code):
    result = cli(isolated, 'just-ask', 'Q', '-m', 'codex', '--task', code)
    assert result.returncode == 2
    assert 'invalid --task' in result.stderr


def test_failed_run_stays_failed(isolated):
    isolated['TEST_PROVIDER_FAIL'] = '1'
    result = cli(isolated, 'just-ask', 'Q', '-m', 'codex', '--pool', '1')
    assert result.returncode == 1, result.stderr
    record = json.loads(cli(isolated, 'jobs', '--json').stdout)[0]
    assert record['status'] == 'failed'
    assert cli(isolated, 'attach', record['job_id'], '--task', 'FAIL-1').returncode == 0
    after = json.loads(cli(isolated, 'status', record['job_id'], '--json').stdout)
    assert after['status'] == 'failed'
    assert after['exit_code'] == 1


@pytest.mark.parametrize('args', [
    ['just-ask', 'Q'], ['quorum', 'Q', '--moderator', 'codex'],
    ['brainstorm', 'Q', '--rounds', '1', '--max-rounds', '1', '--moderator', 'codex'],
])
def test_text_modes_without_task(isolated, args):
    result = cli(isolated, *args, '-m', 'codex', '--pool', '1')
    assert result.returncode == 0, result.stderr
    record = json.loads(cli(isolated, 'jobs', '--json').stdout)[0]
    assert record['output'] == result.stdout
    assert record['status'] == 'done'
    assert record['evidence'][0]['run_id'] == record['job_id']


def test_task_and_no_task_plan_same_roles(isolated):
    first = cli(isolated, 'just-ask', 'Q', '-m', 'codex')
    second = cli(isolated, 'just-ask', 'Q', '-m', 'codex', '--task', 'ROLE-1')
    assert first.returncode == second.returncode == 0
    records = json.loads(cli(isolated, 'jobs', '--json').stdout)
    assert len(records) == 2
    evidence = [r['evidence'][0] for r in records]
    assert len(evidence[0]['roles']) == 3
    assert evidence[0]['roles'] == evidence[1]['roles']
    assert evidence[0]['models'] == evidence[1]['models']


def test_env_task_and_explicit_precedence(isolated):
    isolated['REVIEW_TASK_CODE'] = 'ENV-1'
    assert cli(isolated, 'just-ask', 'Q', '-m', 'codex', '--pool', '1').returncode == 0
    isolated['REVIEW_TASK_CODE'] = 'invalid env'
    assert cli(isolated, 'just-ask', 'Q', '-m', 'codex', '--pool', '1', '--task', 'EXPLICIT-1').returncode == 0
    assert cli(isolated, 'just-ask', 'Q', '-m', 'codex').returncode == 2
    rows = [json.loads(line) for line in Path(isolated['REVIEW_STATS_FILE']).read_text().splitlines()]
    assert {r['task_code'] for r in rows} == {'ENV-1', 'EXPLICIT-1'}


@pytest.mark.parametrize('run_id', ['../outside', '/tmp/outside', '20260914T120000-00000000', '20260914'])
def test_missing_invalid_or_partial_id(isolated, run_id):
    result = cli(isolated, 'attach', run_id, '--task', 'TASK-1')
    assert result.returncode == 2


def test_attachment_concurrent_conflict_and_append(isolated):
    from concurrent.futures import ThreadPoolExecutor
    assert cli(isolated, 'just-ask', 'Q', '-m', 'codex', '--pool', '1').returncode == 0
    run_id = json.loads(cli(isolated, 'jobs', '--json').stdout)[0]['job_id']
    with ThreadPoolExecutor(max_workers=3) as pool:
        actions = [pool.submit(cli, isolated, 'attach', run_id, '--task', code) for code in ['A', 'B']]
        new_run = pool.submit(cli, isolated, 'just-ask', 'Q2', '-m', 'codex', '--pool', '1')
        assert sorted(a.result().returncode for a in actions) == [0, 2]
        assert new_run.result().returncode == 0
    rows = json.loads(cli(isolated, 'jobs', '--json').stdout)
    assert len(rows) == 2
    assert sum(len(r['evidence']) for r in rows) == 2


def test_detached_uses_same_identity(isolated):
    detached = cli(isolated, 'just-ask', 'Q', '-m', 'codex', '--pool', '1', '--detach')
    assert detached.returncode == 0, detached.stderr
    records = json.loads(cli(isolated, 'jobs', '--json').stdout)
    run_id = records[0]['job_id']
    waited = cli(isolated, 'wait', run_id, '--timeout', '15')
    assert waited.returncode == 0, waited.stderr
    record = json.loads(cli(isolated, 'status', run_id, '--json').stdout)
    assert record['job_id'] == run_id
    assert 'output' in record
    assert len(json.loads(cli(isolated, 'jobs', '--json').stdout)) == 1
    assert cli(isolated, 'attach', run_id, '--task', 'DETACHED-1', '--json').returncode == 0


def test_management_does_not_create_runs(isolated):
    for args in [('jobs', '--json'), ('just-ask', '--help'), ('--help',), ('task', '--json')]:
        assert cli(isolated, *args).returncode == 0
    assert json.loads(cli(isolated, 'jobs', '--json').stdout) == []


def test_attachment_crash_before_replace(isolated, monkeypatch):
    from reviewlib import jobs
    monkeypatch.setenv('REVIEW_JOBS_DIR', isolated['REVIEW_JOBS_DIR'])
    monkeypatch.setenv('HOME', isolated['HOME'])
    run_id = jobs.new_job_id()
    jobs.write_job(run_id, status='failed', output='original', exit_code=1, started_at=1, finished_at=2)
    original = jobs.job_path(run_id).read_bytes()
    def fail_replace(*args):
        raise OSError('simulated crash before atomic replacement')
    monkeypatch.setattr(jobs.os, 'replace', fail_replace)
    with pytest.raises(OSError, match='simulated crash'):
        jobs.attach_task(run_id, 'TASK-1')
    assert jobs.job_path(run_id).read_bytes() == original


def test_diff_identity_survives_later_working_tree(isolated, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    subprocess.run(['git', 'init', '-q', str(project)], check=True, env=isolated, timeout=10)
    subprocess.run(['git', '-C', str(project), 'remote', 'add', 'origin', 'https://example.test/team/project.git'], check=True, env=isolated, timeout=10)
    diff = 'diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n'
    result = subprocess.run([sys.executable, str(ROOT / 'bin/review'), 'diff', '-C', str(project), '-m', 'codex', '--pool', '1'], env=isolated, input=diff, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    record = json.loads(cli(isolated, 'jobs', '--json').stdout)[0]
    before = record['evidence']
    (project / 'a.py').write_text('unrelated current content')
    assert cli(isolated, 'attach', record['job_id'], '--task', 'DIFF-1').returncode == 0
    after = json.loads(cli(isolated, 'status', record['job_id'], '--json').stdout)
    assert after['evidence'] == before
    assert before[0]['diff_files'] == ['a.py']
    assert before[0]['diff_sha256']
    assert before[0]['repo_id'] == 'example.test/team/project'


def test_evidence_survives_missing_stats_index(isolated):
    assert cli(isolated, 'just-ask', 'Q', '-m', 'codex', '--pool', '1').returncode == 0
    record = json.loads(cli(isolated, 'jobs', '--json').stdout)[0]
    Path(isolated['REVIEW_STATS_FILE']).unlink()
    assert cli(isolated, 'attach', record['job_id'], '--task', 'RECOVER-1').returncode == 0
    evidence = cli(isolated, 'task', 'RECOVER-1', '--json')
    assert evidence.returncode == 0
    assert record['job_id'] in evidence.stdout


def test_cannot_reuse_completed_run_environment(isolated):
    assert cli(isolated, 'just-ask', 'Q', '-m', 'codex', '--pool', '1').returncode == 0
    record = json.loads(cli(isolated, 'jobs', '--json').stdout)[0]
    isolated['REVIEW_JOB_ID'] = record['job_id']
    assert cli(isolated, 'just-ask', 'different question', '-m', 'codex').returncode == 2
    isolated.pop('REVIEW_JOB_ID')
    assert json.loads(cli(isolated, 'status', record['job_id'], '--json').stdout) == record


def test_attach_rejects_live_and_ambiguous_records(isolated, monkeypatch, tmp_path):
    from reviewlib import jobs
    monkeypatch.setenv('REVIEW_JOBS_DIR', isolated['REVIEW_JOBS_DIR'])
    run_id = jobs.new_job_id()
    jobs.write_job(run_id, status='running', output='partial')
    with pytest.raises(ValueError, match='completion'):
        jobs.attach_task(run_id, 'TASK-1')
    jobs.write_job(run_id, status='failed')
    fallback = tmp_path / 'fallback'
    fallback.mkdir()
    (fallback / f'{run_id}.json').write_bytes(jobs.job_path(run_id).read_bytes())
    monkeypatch.setattr(jobs, '_secondary_jobs_dir', lambda: fallback)
    with pytest.raises(ValueError, match='ambiguous'):
        jobs.attach_task(run_id, 'TASK-1')


def test_attach_rejects_symlink_and_mismatched_identity(isolated, monkeypatch, tmp_path):
    from reviewlib import jobs
    monkeypatch.setenv('REVIEW_JOBS_DIR', isolated['REVIEW_JOBS_DIR'])
    run_id = jobs.new_job_id()
    target = tmp_path / 'foreign.json'
    target.write_text(json.dumps({'job_id': run_id, 'status': 'done', 'output': 'foreign'}))
    path = jobs.job_path(run_id)
    path.symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        jobs.attach_task(run_id, 'TASK-1')
    path.unlink()
    data = json.loads(target.read_text())
    data['job_id'] = jobs.new_job_id()
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='not found'):
        jobs.attach_task(run_id, 'TASK-1')
    assert 'task_code' not in json.loads(target.read_text())


def test_visual_json_and_attachment_use_real_image(isolated, tmp_path):
    from PIL import Image, ImageDraw
    image_path = tmp_path / 'screen.png'
    image = Image.new('RGB', (800, 600), '#e9eef7')
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 780, 100), fill='#184687')
    for y in range(140, 530, 100):
        draw.rectangle((30, y, 760, y + 80), fill='white', outline='#8398b7')
        draw.text((55, y + 20), 'Review status: completed with evidence', fill='#123456')
    image.save(image_path)
    isolated.pop('REVIEW_FAKE_BACKEND')
    marker = tmp_path / 'vision-called'
    isolated['TEST_VISION'] = str(marker)
    result = cli(isolated, 'visual', str(image_path), '-m', 'codex', '--json', '--no-local-model')
    payload = json.loads(result.stdout)
    assert payload['vision_verdict'] == 'keep', result.stdout + result.stderr
    assert marker.read_text() == 'received image'
    record = json.loads(cli(isolated, 'jobs', '--json').stdout)[0]
    assert json.loads(record['output']) == payload
    assert record['job_id'] in result.stderr
    assert cli(isolated, 'attach', record['job_id'], '--task', 'IMAGE-1').returncode == 0


def test_call_log_reader_resolves_late_task(isolated):
    assert cli(isolated, 'just-ask', 'Q', '-m', 'codex', '--pool', '1').returncode == 0
    record = json.loads(cli(isolated, 'jobs', '--json').stdout)[0]
    assert cli(isolated, 'attach', record['job_id'], '--task', 'LOG-1').returncode == 0
    detail = cli(isolated, 'task', 'LOG-1', '--detail', '1', '--json')
    assert detail.returncode == 0, detail.stderr
    assert 'test-provider evidence' in detail.stdout


def test_qa_handler_without_task(isolated, tmp_path):
    sut = tmp_path / 'sut'
    suites = sut / 'docs/tests/suites'
    suites.mkdir(parents=True)
    (suites / 'smoke.md').write_text('# Suite: smoke\n\n## Case: reads input\nSteps:\n- read input\nExpected:\n- input visible\n')
    env = dict(isolated, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.test',
               GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.test',
               REVIEW_QA_FAKE_TESTER='1', REVIEW_QA_FAKE_VERDICT='PASS')
    for args in [('init', '-q'), ('add', '.'), ('commit', '-qm', 'test: create fixture')]:
        subprocess.run(['git', '-C', str(sut), *args], check=True, env=env, timeout=15,
                       capture_output=True, text=True)
    result = cli(env, 'qa', str(sut), '-m', 'codex', '--report', str(tmp_path / 'qa-report.md'))
    assert result.returncode == 0, result.stderr
    record = json.loads(cli(env, 'jobs', '--json').stdout)[0]
    assert record['status'] == 'done'
    assert 'VERDICT=PASS' in record['stderr']
    assert cli(env, 'attach', record['job_id'], '--task', 'QA-1').returncode == 0


def test_output_failure_is_not_credited_after_attachment(isolated, tmp_path):
    result = cli(isolated, 'just-ask', 'Q', '-m', 'codex', '--pool', '1', '-o', str(tmp_path))
    assert result.returncode == 1
    run = json.loads(cli(isolated, 'jobs', '--json').stdout)[0]
    assert run['status'] == 'failed'
    assert run['evidence'][0]['passed'] is True
    assert cli(isolated, 'attach', run['job_id'], '--task', 'OUTPUT-1').returncode == 0
    checked = cli(isolated, 'task', 'OUTPUT-1', '--check', '--min-iter', '1', '--min-models', '1', '--json')
    assert checked.returncode != 0
    assert json.loads(checked.stdout)['passed_iterations'] == 0
    after = json.loads(cli(isolated, 'status', run['job_id'], '--json').stdout)
    assert after['evidence'] == run['evidence']


def test_leading_options_keep_run_identity(isolated):
    result = cli(isolated, '-m', 'codex', '--pool', '1', 'just-ask', 'Q')
    assert result.returncode == 0, result.stderr
    assert len(json.loads(cli(isolated, 'jobs', '--json').stdout)) == 1


def test_attach_unique_fallback_in_place(isolated, monkeypatch, tmp_path):
    from reviewlib import jobs
    monkeypatch.setenv('REVIEW_JOBS_DIR', isolated['REVIEW_JOBS_DIR'])
    fallback = tmp_path / 'fallback'
    fallback.mkdir()
    run_id = jobs.new_job_id()
    path = fallback / f'{run_id}.json'
    path.write_text(json.dumps({'job_id': run_id, 'status': 'failed', 'output': 'partial', 'exit_code': 1}))
    monkeypatch.setattr(jobs, '_secondary_jobs_dir', lambda: fallback)
    assert jobs.attach_task(run_id, 'FALLBACK-1')
    assert json.loads(path.read_text())['task_code'] == 'FALLBACK-1'
    assert not jobs.job_path(run_id).exists()
