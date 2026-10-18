"""Native Harness2 substrate/benchmark for an isolated, synthetic pilot."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
import subprocess
from pathlib import Path

from ..core import runmark, tree
from ..core.spec import BenchPrompts, BenchSpec, ChannelSpec
from ..substrate.base import Install, RunRequest
from .gateway import MODEL, Refused
from .worker import run_isolated

FILES = ('systemprompt.md', 'tool_descriptions/tool_guidance.md')
SURFACE = tree.SurfaceSpec(name='hybrid', always_on=FILES, always_on_max=12000,
                          always_on_cap_reason='pilot limit', skill_dir='skills', skill_name_rule='must_match',
                          trigger_hint='', autotrigger_file='systemprompt.md', trigger_line='',
                          subagent_requires_mode=True, plugin_dir=None, hooks_file=None)
BASE = {'systemprompt.md': 'Read the task input, preserve its signs and write the requested artifact.\n',
        'tool_descriptions/tool_guidance.md': 'Use the available read and write tools. Inspect your output before finishing.\n'}
TASKS = {
    'ledger-train': {'csv': 'amount\n100.25\n-20.10\n5.00\n', 'expected': '85.15'},
    'ledger-heldout': {'csv': 'amount\n-35.50\n10.25\n5.00\n0.25\n', 'expected': '-20.00'},
}
PROMPT = ('Read ledger.csv in the current directory. Sum ALL signed amounts exactly, including negative '
          'amounts and zeros. Write answer.json containing exactly one field total, a string with two '
          'decimal places. Preserve ledger.csv. Use tools to read the source and write the artifact; '
          'a final answer without answer.json does not complete this task. Check your artifact before finishing.')


def validate_candidate(candidate):
    if not isinstance(candidate, dict) or not candidate or set(candidate) - set(FILES):
        raise Refused('only the two admitted prompt files may change')
    if any(not isinstance(v, str) for v in candidate.values()):
        raise Refused('prompt files must be text')
    if sum(len(v) for v in candidate.values()) > 12000 or sum(len(v.encode()) for v in candidate.values()) > 65536:
        raise Refused('prompt candidate exceeds size limit')
    return candidate


def read_candidate(root):
    root = Path(root)
    contents = {}
    for path in root.rglob('*'):
        if path.is_symlink():
            raise Refused('candidate symlink refused')
        if path.is_file():
            contents[path.relative_to(root).as_posix()] = path.read_text(encoding='utf-8')
    return validate_candidate(contents)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


class HybridSubstrate:
    def __init__(self, name, launcher):
        self.name = name
        self.launcher = launcher

    def surface(self):
        return SURFACE

    def materialise(self, tree_dir, spec, ctx):
        if ctx:
            raise Refused('arbitrary context injection excluded')
        candidate = read_candidate(tree_dir)
        dest = Path(tempfile.mkdtemp(prefix='hybrid-harness-'))
        tree.write_tree(SURFACE, candidate, dest, stubs=False)
        return Install(dest, runmark.fingerprint(Path(tree_dir)), {'sha256': digest(candidate)})

    def execute(self, task_ref, install, run_ids, timeout):
        return self.launcher(RunRequest(task_ref, tuple(run_ids), timeout), install)

    def read_trajectory(self, stream_path, *, obs_trunc, max_chars, args_trunc=400):
        return Path(stream_path).read_text()[:min(obs_trunc, max_chars)]

    def preflight(self):
        if shutil.which('bwrap') is None:
            raise RuntimeError('bubblewrap is required; no host fallback')


def assess(task_id, work, *, worker_success):
    answer = Path(work) / 'answer.json'
    delivered = answer.is_file() and not answer.is_symlink() and answer.stat().st_size <= 4096
    correct = False
    if delivered:
        try:
            value = json.loads(answer.read_text())
            correct = value == {'total': TASKS[task_id]['expected']}
        except (ValueError, OSError):
            pass
    source = Path(work) / 'ledger.csv'
    preserved = (not source.is_symlink() and source.is_file() and
                 source.read_text() == TASKS[task_id]['csv'])
    return {'delivered': delivered, 'predicate': correct and preserved,
            'verified': worker_success and correct and preserved,
            'false_success': worker_success and not (correct and preserved)}


def opencode_result(stdout, exit_code):
    events = []
    try:
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        if any(not isinstance(event, dict) for event in events):
            raise ValueError()
    except (ValueError, TypeError):
        return {'success': False, 'outcome': 'invalid_events', 'response': ''}
    text = '\n'.join(str(e.get('part', {}).get('text', '')) for e in events if e.get('type') == 'text')
    terminal = any(e.get('type') == 'step_finish' and e.get('part', {}).get('reason') in ('stop', 'end_turn') for e in events)
    fatal = any(e.get('type') == 'error' for e in events)
    unresolved = set()
    for event in events:
        if event.get('type') != 'tool_use':
            continue
        part = event.get('part') or {}
        state = part.get('state') or {}
        args = state.get('input') or {}
        identity = (part.get('tool'), args.get('filePath') or args.get('filepath') or json.dumps(args, sort_keys=True))
        if state.get('status') == 'error':
            unresolved.add(identity)
            reason = str(state.get('error', '')).lower()
            if part.get('tool') != 'read' or 'file not found' not in reason or any(word in reason for word in ('permission', 'denied', 'rejected', 'forbidden', 'not allowed', 'refused')):
                fatal = True
        elif state.get('status') == 'completed':
            unresolved.discard(identity)
    errors = fatal or bool(unresolved)
    ok = exit_code == 0 and terminal and not errors
    return {'success': ok, 'outcome': 'success' if ok else 'worker_failed', 'response': text}


def chuck_result(stdout, exit_code):
    try:
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            raise ValueError()
    except ValueError:
        return {'success': False, 'outcome': 'invalid_events', 'response': ''}
    accepted = (exit_code == 0 and payload.get('success') is True and
                payload.get('outcome') in {'success', 'direct_response'})
    if payload.get('success') and not accepted:
        payload['outcome'] = 'inconsistent_result' if exit_code == 0 else 'process_failed'
    payload['success'] = accepted
    return payload


def render_trajectory(profile, events):
    header = ('[TRACE COVERAGE] Every captured event is included. Chuck tool records contain '
              'argument hashes and bounded result prefixes, not full tool arguments/output.\n')
    return header + '\n'.join('[EVENT] ' + json.dumps(event, ensure_ascii=True) for event in events)


def execution_sources():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(__file__).parent.glob('*.py'))}


def _source_pin(root, expected, *, allow_patch=False):
    head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if head != expected:
        raise Refused('runtime source moved from the reviewed pin')
    changed = subprocess.check_output(['git', '-C', str(root), 'diff', '--name-only', 'HEAD'], text=True).splitlines()
    allowed = {'bun.lock', 'packages/app/package.json', 'packages/opencode/src/provider/transform.ts',
               'packages/opencode/src/cli/cmd/run.ts'}
    if changed and (not allow_patch or set(changed) != allowed):
        raise Refused('unexpected runtime source changes')
    diff = subprocess.check_output(['git', '-C', str(root), 'diff', '--binary', 'HEAD'])
    if allow_patch:
        patches = Path(__file__).resolve().parents[2] / 'third_party/patches'
        approved = b''.join((patches / name).read_bytes() for name in
                            ('opencode-compat.diff', 'hybrid-opencode-events.diff'))
        normalize = lambda value: b'\n'.join(line for line in value.splitlines() if not line.startswith(b'index '))
        # Git orders paths; the two independently retained patches need the same ordering.
        sections = lambda value: sorted(normalize(b'diff --git ' + section) for section in value.split(b'diff --git ')[1:])
        if sections(diff) != sections(approved):
            raise Refused('OpenCode changes do not equal the approved compatibility patch')
    return {'head': head, 'patch_sha256': hashlib.sha256(diff).hexdigest()}


class WorkerExecutor:
    def __init__(self, gateway, *, oc_root, bun, chuck_root, hub_root, chuck_python, chuck_runtime):
        self.gateway = gateway
        self.oc_root = Path(oc_root).resolve()
        self.bun = Path(bun).resolve()
        self.chuck_root, self.hub_root = Path(chuck_root).resolve(), Path(hub_root).resolve()
        self.chuck_python, self.chuck_runtime = Path(chuck_python), Path(chuck_runtime).resolve()
        self.lock = threading.Lock()
        self.identity = self._snapshot_identity()

    def _snapshot_identity(self):
        return {
            'chuck': _source_pin(self.chuck_root, 'b5c0ef2bb5f21061e32b4422454095b3adb56746'),
            'hub': _source_pin(self.hub_root, '03db5f7f8de333b2ac1b6320873fe24db501a483'),
            'opencode': _source_pin(self.oc_root, '23fb5e0516c99ac04a1aa46c193efda2e1b9bb24', allow_patch=True),
            'bun_sha256': hashlib.sha256(self.bun.read_bytes()).hexdigest(),
        }

    def revalidate(self):
        if self.identity != self._snapshot_identity():
            raise Refused('runtime source identity changed since admission')

    def __call__(self, profile, task, work, candidate, run_id):
        with self.lock:
            self.revalidate()
            if getattr(self.gateway, 'halted', False):
                raise Refused('experiment halted after a false success')
            session_id = f'{profile}:{run_id}'
            token = self.gateway.token(session_id)
            (work / 'AGENTS.md').write_text('\n\n'.join(candidate.values()))
            (work / 'task.txt').write_text(PROMPT)
            common = {'CHUCK_MEMORY': 'off', 'CHUCK_LEARNING': 'off', 'CHUCK_HEART_MIDRUN': 'off',
                      'CYCLONE_URL': 'http://127.0.0.1:1', 'REDIS_URL': 'redis://127.0.0.1:1',
                      'QDRANT_URL': 'http://127.0.0.1:1'}
            if profile == 'chuck':
                env = {**common, 'CHUCK_STATE_DIR': '/tmp/chuck', 'CHUCK_API_MAX_RETRIES': '0',
                       'CHUCK_MAX_USD': '.25', 'CHUCK_MAX_SECONDS': '240', 'CN_CLI_MAX_TURNS': '8',
                       'CHUCK_MAX_OUTPUT_TOKENS': '2048', 'CHUCK_MAX_PROMPT_TOKENS': '30000'}
                command = [str(self.chuck_python), '/bridge/chuck_entry.py', str(self.chuck_root), str(self.hub_root)]
                mounts = [self.chuck_python.parent.parent, self.chuck_runtime, self.chuck_root, self.hub_root]
                config = (self.chuck_python.parent.parent / 'pyvenv.cfg').read_text()
                alias = next(line.split('=', 1)[1].strip() for line in config.splitlines() if line.startswith('home ='))
                mounts.append(Path(alias).parent)
            elif profile == 'oc-v1':
                configuration = {
                    'model': f'anthropic/{MODEL}', 'share': 'disabled', 'snapshot': False,
                    'provider': {'anthropic': {'npm': '@ai-sdk/anthropic',
                        'options': {'baseURL': '__GATEWAY__/v1', 'maxRetries': 0},
                        'models': {MODEL: {'name': 'Claude Haiku 5.5', 'limit': {'context': 100000, 'output': 2048},
                                          'cost': {'input': .1, 'output': .5}, 'reasoning': False}}}},
                    'agent': {'build': {'steps': 8}},
                    'permission': {'*': 'deny', 'read': 'allow', 'edit': 'allow', 'glob': 'allow', 'grep': 'allow'},
                    'tools': {'bash': False, 'webfetch': False, 'websearch': False, 'task': False,
                              'question': False, 'skill': False, 'todowrite': False},
                }
                env = {**common, 'OPENCODE_CONFIG_CONTENT': json.dumps(configuration),
                       'OPENCODE_DISABLE_DEFAULT_PLUGINS': '1', 'OPENCODE_DISABLE_CLAUDE_CODE': '1',
                       'OPENCODE_DISABLE_AUTOUPDATE': '1', 'OPENCODE_DISABLE_MODELS_FETCH': '1',
                       'OPENCODE_DISABLE_LSP_DOWNLOAD': '1', 'OPENCODE_DISABLE_AUTOCOMPACT': '1',
                       'OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER': '1',
                       'OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX': '2048'}
                command = [str(self.bun), 'run', '--cwd', str(self.oc_root / 'packages/opencode'),
                           '--conditions=browser', 'src/index.ts', 'run', PROMPT, '--dir', '/work',
                           '--model', f'anthropic/{MODEL}', '--title', 'synthetic-hybrid-pilot', '--format', 'json']
                mounts = [self.oc_root, self.bun.parent]
            else:
                raise Refused('unsupported worker profile')
            result = run_isolated(command, work, mounts=mounts, environment=env,
                                  gateway=self.gateway, token=token, timeout=300)
            if profile == 'chuck':
                parsed = chuck_result(result['stdout'], result['exit_code'])
            else:
                parsed = opencode_result(result['stdout'], result['exit_code'])
            if result['outcome'] != 'completed':
                parsed['success'] = False
                parsed['outcome'] = result['outcome']
            if profile == 'chuck':
                event_path = work / 'events.jsonl'
                events = [json.loads(line) for line in event_path.read_text().splitlines()] if event_path.exists() else []
            else:
                events = []
                for line in result['stdout'].splitlines():
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        events.append({'type': 'invalid_json_line', 'text': line})
            if assess(task, work, worker_success=bool(parsed.get('success')))['false_success']:
                self.gateway.halt('worker claimed success but artifact verification failed')
            return {**result, **parsed, 'events': events, 'usd': self.gateway.ledger.sessions.get(session_id, 0)}


class HybridAdapter:
    def __init__(self, root, profile, *, executor):
        self.root = Path(root)
        self.executor = executor
        self.profile = profile
        self.substrate = HybridSubstrate(profile, self._launch)
        channel = ChannelSpec('anthropic_api', MODEL, timeout=60, steps=1)
        self.spec = BenchSpec(name='synthetic', runs_root=self.root / 'runs', install_mode='hybrid_prompt',
            prompts=BenchPrompts(work='reconcile signed amounts and deliver a JSON file',
                                inv_levers='Only change the two admitted prompt text files.',
                                dom_levers='Preserve signs and exact decimal arithmetic.'),
            channels={'proposer': channel, 'selector': channel}, allow_subagents=False)
        tree.write_tree(SURFACE, BASE, self.base_harness(), stubs=False)
        source = self.task_files('ledger-train')
        source.mkdir(parents=True, exist_ok=True)
        (source / 'ledger.csv').write_text(TASKS['ledger-train']['csv'])
        self.execution_count = 0

    def tasks(self, split='all'):
        return ['ledger-train']

    def domain_of(self, task_id):
        return 'arithmetic'

    def task_prompt(self, task_id):
        return PROMPT

    def task_files(self, task_id):
        if task_id != 'ledger-train':
            raise Refused('held-out tasks excluded from training context')
        return self.root / 'sources' / task_id

    def base_harness(self):
        return self.root / 'base'

    def validate_tree(self, candidate):
        validate_candidate(candidate)

    def _launch(self, request, install):
        return [self.execute(request.task_ref, install.staging_dir, rid, timeout=request.timeout)
                for rid in request.run_ids]

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        if (self.root / 'STOPPED.json').exists():
            raise Refused('experiment previously halted')
        if task_id not in TASKS or not run_id or '..' in Path(run_id).parts or Path(run_id).is_absolute():
            raise Refused('invalid task or run identity')
        candidate = read_candidate(harness_dir or self.base_harness())
        revalidate = getattr(self.executor, 'revalidate', None)
        if callable(revalidate):
            revalidate()
        identity = digest({'task': TASKS[task_id]['csv'], 'prompt': PROMPT, 'candidate': candidate,
                           'profile': self.profile, 'model': MODEL, 'policy': 'pilot-read-write-v1',
                           'runtime': getattr(self.executor, 'identity', {}),
                           'execution_sources': execution_sources()})
        run_dir = self.root / 'executions' / run_id
        record = run_dir / 'attempt.json'
        if record.exists():
            prior = json.loads(record.read_text())
            if prior.get('false_success'):
                raise Refused('cached false success remains refused')
            if prior['identity'] != identity:
                raise Refused('run identity differs; refuse automatic paid replay')
            if prior.get('artifact_sha256') != self._artifact_hash(run_dir / 'work'):
                raise Refused('artifact drift; refuse automatic paid replay')
            if (run_dir / 'work/ledger.csv').is_symlink() or not (run_dir / 'work/ledger.csv').is_file() or (run_dir / 'work/ledger.csv').read_text() != TASKS[task_id]['csv']:
                raise Refused('source drift; refuse automatic paid replay')
            return run_dir
        if run_dir.exists():
            raise Refused('unfinished run requires reconciliation')
        work = run_dir / 'work'
        work.mkdir(parents=True)
        (work / 'ledger.csv').write_text(TASKS[task_id]['csv'])
        result = self.executor(self.profile, task_id, work, candidate, run_id)
        assessment = assess(task_id, work, worker_success=bool(result['success']))
        payload = {**result, **assessment, 'identity': identity, 'profile': self.profile,
                   'task_id': task_id, 'run_id': run_id, 'artifact_sha256': self._artifact_hash(work)}
        record.write_text(json.dumps(payload, indent=2))
        # Recorded events only; never manufacture tool actions from a task description.
        trajectory = render_trajectory(self.profile, result.get('events', []))
        (run_dir / 'trajectory.md').write_text(trajectory)
        self.execution_count += 1
        if assessment['false_success']:
            gateway = getattr(self.executor, 'gateway', None)
            if gateway:
                gateway.halt('false success recorded')
            (self.root / 'STOPPED.json').write_text(json.dumps({'run_id': run_id, 'reason': 'false_success'}))
            raise Refused('false success recorded; stop experiment')
        return run_dir

    @staticmethod
    def _artifact_hash(work):
        path = Path(work) / 'answer.json'
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 4096:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def delivered(self, run_dir):
        row = self._checked_record(run_dir)
        return row['delivered'] and row['success']

    def _checked_record(self, run_dir):
        run_dir = Path(run_dir)
        row = json.loads((run_dir / 'attempt.json').read_text())
        source = run_dir / 'work/ledger.csv'
        if source.is_symlink() or not source.is_file() or source.read_text() != TASKS[row['task_id']]['csv']:
            raise Refused('source drift in recorded result')
        if row['artifact_sha256'] != self._artifact_hash(run_dir / 'work'):
            raise Refused('deliverable drift in recorded result')
        return row

    def trajectory(self, run_dir, *, obs_trunc):
        # Typed records already bound tool output; keep every captured event.
        return (Path(run_dir) / 'trajectory.md').read_text()

    def deliverable_text(self, run_dir):
        path = Path(run_dir) / 'work/answer.json'
        return path.read_text()[:4096] if path.is_file() and not path.is_symlink() else ''

    def deliverable_files(self, run_dir):
        return {'answer.json': self.deliverable_text(run_dir)}

    def score(self, run_dir, task_id):
        row = self._checked_record(run_dir)
        return {'n_passed': int(row['verified']), 'n_criteria': 1, 'all_pass': row['verified']}
