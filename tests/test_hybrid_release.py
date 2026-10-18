"""Release-review regressions: durability, provenance and terminal truth."""
import importlib.metadata
import json
import os
import stat
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from harness2.integrations import gateway, hybrid, pilot
from harness2.integrations.anthropic_channel import request_json


class ReleaseTests(unittest.TestCase):
    def test_first_reservation_and_created_directories_are_durable_before_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'run-001'
            seen = []
            actual_fsync = os.fsync
            def fsync(fd):
                seen.append((stat.S_ISDIR(os.fstat(fd).st_mode), Path(os.readlink(f'/proc/self/fd/{fd}'))))
                return actual_fsync(fd)
            def transport(body, headers):
                self.assertIn((False, root / 'gateway/receipts.jsonl'), seen)
                for directory in (root, root.parent, root / 'gateway'):
                    self.assertIn((True, directory), seen)
                raw = {'id': 'fixture', 'model': gateway.MODEL, 'stop_reason': 'end_turn',
                       'content': [{'type': 'text', 'text': 'ok'}],
                       'usage': {'input_tokens': 2, 'output_tokens': 1}}
                return 200, {'request-id': 'fixture'}, json.dumps(raw).encode()
            with mock.patch.object(os, 'fsync', side_effect=fsync):
                with gateway.Gateway(root / 'gateway', transport=transport) as broker:
                    response = request_json(broker, 'fixture', {'model': gateway.MODEL, 'max_tokens': 10,
                        'messages': [{'role': 'user', 'content': 'hello'}]})
                    self.assertEqual(response['id'], 'fixture')

    def test_previous_admitted_run_without_journal_refuses_new_spend(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / 'run-001'
            run.mkdir()
            (run / 'experiment.json').write_text('{}')
            with self.assertRaises(gateway.Refused):
                pilot.prior_spend(root)

    def test_manifest_does_not_require_sdk_in_controller_environment(self):
        def version(name):
            if name == 'anthropic':
                raise importlib.metadata.PackageNotFoundError(name)
            return '0.1.0'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(importlib.metadata, 'version', side_effect=version), \
                 mock.patch.object(pilot, 'Gateway', side_effect=gateway.Refused('fixture admission stop')):
                with self.assertRaisesRegex(gateway.Refused, 'fixture admission stop'):
                    pilot.run(root / 'run-001', key='synthetic', credential_source='fixture', lab=root)
            manifest = json.loads((root / 'run-001/experiment.json').read_text())
            self.assertIsNone(manifest['python_packages']['anthropic'])

    def test_helper_source_change_invalidates_cached_attempt(self):
        def execute(profile, task, work, candidate, run_id):
            (work / 'answer.json').write_text('{"total":"85.15"}')
            return {'success': True, 'outcome': 'success', 'usd': 0, 'events': []}
        with tempfile.TemporaryDirectory() as temp:
            adapter = hybrid.HybridAdapter(Path(temp), 'chuck', executor=execute)
            with mock.patch.object(hybrid, 'execution_sources', return_value={'worker.py': 'old'}, create=True):
                adapter.execute('ledger-train', None, 'same')
            with mock.patch.object(hybrid, 'execution_sources', return_value={'worker.py': 'changed'}, create=True):
                with self.assertRaises(gateway.Refused):
                    adapter.execute('ledger-train', None, 'same')

    def test_runtime_drift_refuses_before_token_or_worker_launch(self):
        executor = hybrid.WorkerExecutor.__new__(hybrid.WorkerExecutor)
        executor.lock = threading.Lock()
        executor.identity = {'source': 'old'}
        executor.gateway = types.SimpleNamespace(halted=False, token=mock.Mock())
        with mock.patch.object(executor, '_snapshot_identity', return_value={'source': 'changed'}, create=True):
            with self.assertRaisesRegex(gateway.Refused, 'source'):
                executor('chuck', 'ledger-train', Path('/not-created'), {}, 'fixture')
        executor.gateway.token.assert_not_called()

    def test_completed_write_does_not_clear_prior_write_failure(self):
        events = [{'type': 'tool_use', 'part': {'tool': 'write', 'state': {'status': status,
                  'input': {'filePath': '/work/x'}, 'error': 'I/O failure'}}}
                  for status in ('error', 'completed')]
        events.append({'type': 'step_finish', 'part': {'reason': 'stop'}})
        self.assertFalse(hybrid.opencode_result('\n'.join(map(json.dumps, events)), 0)['success'])
