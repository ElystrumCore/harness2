"""Bounded synthetic experiment. --plan is offline; --live is explicit."""
from __future__ import annotations

import argparse
import fcntl
import importlib.metadata
import json
import os
import time
from pathlib import Path

from ..core import recursion, schedule
from .anthropic_channel import HybridApiProposer, request_json
from .gateway import Gateway, Ledger, MODEL, Refused
from .hybrid import BASE, TASKS, HybridAdapter, WorkerExecutor, digest, read_candidate
from .runtime import load_runtime


def plan():
    return {'schema_version': 1, 'model': MODEL, 'total_cap_usd': 5.0, 'session_cap_usd': .25,
            'request_timeout_seconds': 60, 'worker_timeout_seconds': 300,
            'concurrency': 1, 'k': 1, 'mode': 'agg',
            'training_tasks': ['ledger-train'], 'heldout_tasks': ['ledger-heldout'],
            'baseline_profiles': ['chuck', 'oc-v1'], 'adaptation_profile': 'chuck',
            'maximum_sessions': 17, 'maximum_session_reservations_usd': 4.25,
            'phases': ['connectivity', 'fixed-baselines', 'harness2-propose-probe-compose',
                       'freeze-candidate-and-library', 'heldout', 'report'],
            'scope': 'synthetic transport/method validation; not a general quality evaluation',
            'credit_route': 'direct Anthropic API; no Vertex or account fallback',
            'memory': 'off', 'learning': 'off', 'release': 'none'}


def prior_spend(lab):
    spent = 0.0
    for root in Path(lab).glob('run-*'):
        if root.is_dir() and next(root.rglob('STOPPED.json'), None) is not None:
            raise Refused('prior experiment halt persists across this series')
    for path in Path(lab).glob('run-*/gateway/receipts.jsonl'):
        ledger = Ledger(path)
        try:
            spent += ledger.spent
        finally:
            ledger.close()
    return spent


def load_key(path):
    value = os.environ.get('ANTHROPIC_API_KEY')
    if value:
        return value, 'environment'
    if path is None:
        raise Refused('no environment credential and no explicit credential file')
    # The existing operator-approved credential source. Never print values/fingerprints.
    with Path(path).open() as source:
        for raw in source:
            line = raw.strip().removeprefix('export ')
            if line.startswith('ANTHROPIC_API_KEY='):
                value = line.split('=', 1)[1].strip().strip('\"\'')
                if value:
                    return value, 'existing approved env file'
    raise Refused('no existing Claude API credential; no account fallback')


def executor_for(gateway, lab):
    paths = load_runtime(Path(lab) / 'runtime.json')
    return WorkerExecutor(gateway, **paths,
        chuck_runtime=paths['chuck_python'].resolve().parent.parent)


def event(phase, **values):
    print(json.dumps({'phase': phase, **values}), flush=True)


def run(output, *, key, credential_source, lab, remaining_cap=5.0, previous_usd=0.0):
    output = Path(output).resolve()
    if output.exists():
        raise Refused('experiment directory already exists; no automatic paid replay')
    output.mkdir(parents=True, mode=0o700)
    manifest = {**plan(), 'started_at': time.time(), 'credential_source': credential_source,
                'total_cap_usd': remaining_cap, 'series_cap_usd': 5.0, 'previous_experiments_usd': previous_usd,
                'task_sha256': {key: digest(value) for key, value in TASKS.items()},
                'base_sha256': digest(BASE), 'pricing_source':
                'https://platform.claude.com/docs/en/about-claude/pricing',
                'pricing_checked_at': '2026-10-18', 'python_packages': {
                    name: importlib.metadata.version(name) for name in ('harness2', 'anthropic')},
                'implementation_sha256': {p.name: __import__('hashlib').sha256(p.read_bytes()).hexdigest()
                    for p in Path(__file__).parent.glob('*.py')}}
    (output / 'experiment.json').write_text(json.dumps(manifest, indent=2))
    gateway = Gateway(output / 'gateway', key=key, total_cap=remaining_cap, session_cap=.25)
    status, failure, method = 'incomplete', None, None
    try:
        executor = executor_for(gateway, lab)
        manifest['runtime_sources'] = executor.identity
        (output / 'experiment.json').write_text(json.dumps(manifest, indent=2))
        response = request_json(gateway, 'connectivity', {
            'model': MODEL, 'max_tokens': 32,
            'messages': [{'role': 'user', 'content': 'Reply exactly READY.'}]})
        (output / 'connectivity.json').write_text(json.dumps(response, indent=2))
        event('connectivity', actual_model=response['model'], metered_usd=gateway.ledger.spent)
        for profile in ('chuck', 'oc-v1'):
            adapter = HybridAdapter(output / 'baseline' / profile, profile, executor=executor)
            for task_id in ('ledger-train', 'ledger-heldout'):
                result = adapter.execute(task_id, None, 'baseline-' + task_id)
                row = json.loads((result / 'attempt.json').read_text())
                event('baseline', profile=profile, task=task_id, outcome=row['outcome'],
                      verified=row['verified'], usd=row['usd'])
                if not row['verified']:
                    raise Refused('baseline did not verify; preserve evidence and stop')
        # Upstream's phase widths are otherwise two; admission and execution remain serial too.
        recursion.PHASE_WIDTH = 1
        schedule.ROLLOUT_PARALLEL = 1
        adapter = HybridAdapter(output / 'hybrid', 'chuck', executor=executor)
        proposer = HybridApiProposer(gateway, output / 'api-sessions')
        method = schedule.run_domain('arithmetic', ['ledger-train'], 1, adapter=adapter,
            prop=proposer, selector_prop=proposer, runs_root=adapter.spec.runs_root,
            library_root=output / 'library', modes=('agg',))
        (output / 'method.json').write_text(json.dumps(method, indent=2))
        event('harness2-method', done=method['done'], failed=method['failed'],
              api_sessions=len(proposer._calls), solver_attempts=adapter.execution_count)
        if method['failed'] or gateway.ledger.pending or getattr(gateway, 'halted', False):
            raise Refused('method incomplete, halted or awaiting provider reconciliation')
        summaries = list(adapter.spec.runs_root.rglob('summary.json'))
        if len(summaries) != 1:
            raise Refused('method summary identity is ambiguous')
        summary = json.loads(summaries[0].read_text())
        selection = summary['selections']['agg']
        if not selection.get('ok'):
            raise Refused('no successful selection')
        candidate = summaries[0].parent / 'harnesses' / selection['chosen_label']
        frozen = {'candidate_sha256': digest(read_candidate(candidate)),
                  'library_sha256': digest({p.relative_to(output / 'library').as_posix(): p.read_text()
                                            for p in (output / 'library').rglob('*') if p.is_file()}),
                  'selection': selection, 'frozen_at': time.time(),
                  'proposal_sessions_ok': sum(reply.ok for reply in proposer._calls),
                  'proposal_sessions_failed': sum(not reply.ok for reply in proposer._calls)}
        (output / 'frozen.json').write_text(json.dumps(frozen, indent=2))
        result = adapter.execute('ledger-heldout', candidate, 'heldout-frozen')
        row = json.loads((result / 'attempt.json').read_text())
        event('heldout', profile='chuck', verified=row['verified'], outcome=row['outcome'], usd=row['usd'])
        if not row['verified']:
            raise Refused('frozen candidate did not verify on heldout')
        status = 'passed' if not frozen['proposal_sessions_failed'] else 'passed_with_proposal_fallbacks'
    except Exception as exc:
        failure = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        event('stopped', reason=failure)
    finally:
        gateway.close()
        rows = [json.loads(path.read_text()) for path in sorted(output.rglob('attempt.json'))]
        report = {'status': status, 'failure': failure, 'scope': manifest['scope'],
                  'attempts': len(rows), 'verified': sum(row['verified'] for row in rows),
                  'false_successes': sum(row['false_success'] for row in rows),
                  'estimated_usd': gateway.ledger.spent, 'provider_receipts': len(gateway.ledger.done),
                  'unresolved_reservations': len(gateway.ledger.pending), 'cap_usd': remaining_cap,
                  'series_cap_usd': 5.0, 'series_estimated_usd': previous_usd + gateway.ledger.spent,
                  'console_balance_checked': False, 'promo_eligibility_verified': False,
                  'rows': [{key: row.get(key) for key in ('run_id', 'task_id', 'profile', 'outcome',
                       'verified', 'false_success', 'usd', 'wall_s')} for row in rows],
                  'method': method}
        (output / 'report.json').write_text(json.dumps(report, indent=2))
        event('report', path=str(output / 'report.json'), status=status,
              verified=report['verified'], attempts=report['attempts'],
              estimated_usd=report['estimated_usd'], unresolved=report['unresolved_reservations'])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--lab-root', type=Path)
    parser.add_argument('--env-file', type=Path, help='Explicit existing credential file; otherwise environment only')
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps(plan(), indent=2))
        return 0
    if args.plan or args.output is None or args.lab_root is None:
        parser.error('--live requires --lab-root and --output and cannot combine with --plan')
    lab = args.lab_root.resolve(strict=True)
    if args.output.resolve().parent != lab or not args.output.name.startswith('run-'):
        parser.error('live output must be a fresh run-* directory directly under the isolated lab')
    fd = os.open(lab / 'series-admission.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = prior_spend(lab)
        remaining = 5.0 - previous
        if remaining < plan()['maximum_session_reservations_usd']:
            raise Refused('remaining series budget cannot reserve the full planned pilot')
        # Validate configuration and source identity before opening any credential source.
        executor_for(None, lab)
        key, source = load_key(args.env_file)
        report = run(args.output, key=key, credential_source=source, lab=lab,
                     remaining_cap=remaining, previous_usd=previous)
    finally:
        os.close(fd)
    return 0 if report['status'].startswith('passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
