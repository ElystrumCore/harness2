import importlib
import json
import tempfile
import unittest
from pathlib import Path

from harness2.substrate.base import Substrate


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('harness2.integrations.hybrid')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_native_substrate_restricts_raw_files_and_byte_limits(self):
        root = self.root / 'tree'
        root.mkdir()
        (root / 'systemprompt.md').write_text('Use read and write tools.')
        substrate = self.module.HybridSubstrate('chuck', lambda *args: [])
        self.assertIsInstance(substrate, Substrate)
        install = substrate.materialise(root, None, {})
        self.assertEqual(len(install.extras['sha256']), 64)
        for name in ('plugin/x.ts', 'scripts/x.py', '../escape'):
            with self.assertRaises(ValueError):
                self.module.validate_candidate({'systemprompt.md': 'ok', name: 'bad'})
        with self.assertRaises(ValueError):
            self.module.validate_candidate({'systemprompt.md': 'x' * 12001})
        (root / 'linked').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            substrate.materialise(root, None, {})

    def test_grading_separates_delivery_outcome_and_predicate(self):
        work = self.root / 'work'
        work.mkdir()
        (work / 'ledger.csv').write_text(self.module.TASKS['ledger-train']['csv'])
        for outcome, answer, expected in (
            ('success', {'total': '85.15'}, True),
            ('success', {'total': 'wrong'}, False),
            ('brain_error', {'total': '85.15'}, False),
        ):
            (work / 'answer.json').write_text(json.dumps(answer))
            row = self.module.assess('ledger-train', work, worker_success=outcome == 'success')
            self.assertEqual(row['verified'], expected)
            self.assertEqual(row['false_success'], outcome == 'success' and not expected)
        (work / 'answer.json').unlink()
        (work / 'answer.json').symlink_to('/etc/passwd')
        self.assertFalse(self.module.assess('ledger-train', work, worker_success=True)['verified'])

    def test_removed_source_is_not_verified(self):
        (self.root / 'answer.json').write_text('{"total":"85.15"}')
        self.assertFalse(self.module.assess('ledger-train', self.root, worker_success=True)['verified'])

    def test_heldout_is_not_exposed_in_training_adapter(self):
        adapter = self.module.HybridAdapter(self.root, 'chuck', executor=lambda *a: None)
        self.assertEqual(adapter.tasks(), ['ledger-train'])
        source = adapter.task_files('ledger-train')
        self.assertEqual([p.name for p in source.iterdir()], ['ledger.csv'])
        self.assertNotIn('85.15', adapter.task_prompt('ledger-train'))
        self.assertNotIn('ledger-heldout', '\n'.join(str(p) for p in source.rglob('*')))

    def test_opencode_parser_refuses_noise_denial_and_missing_terminal(self):
        parse = self.module.opencode_result
        events = [{'type': 'text', 'part': {'text': 'done'}},
                  {'type': 'step_finish', 'part': {'reason': 'stop', 'cost': 0.002}}]
        self.assertTrue(parse('\n'.join(json.dumps(e) for e in events), 0)['success'])
        self.assertFalse(parse('banner\n' + json.dumps(events[-1]), 0)['success'])
        self.assertFalse(parse(json.dumps(events[0]), 0)['success'])
        self.assertFalse(parse(json.dumps({'type': 'error', 'error': {'message': 'denied'}}), 0)['success'])

    def test_observed_opencode_read_race_recovery_has_a_valid_terminal_success(self):
        fixture = Path(__file__).parent / 'fixtures/hybrid/opencode-recovered-read.jsonl'
        self.assertTrue(self.module.opencode_result(fixture.read_text(), 0)['success'])

    def test_opencode_denial_or_unresolved_failure_remains_a_failure(self):
        def stream(tool, error, later=None):
            events = [{'type': 'tool_use', 'part': {'tool': tool, 'state': {
                'status': 'error', 'input': {'filePath': '/work/x'}, 'error': error}}}]
            if later:
                events.append({'type': 'tool_use', 'part': {'tool': later, 'state': {
                    'status': 'completed', 'input': {'filePath': '/work/x'}, 'output': 'ok'}}})
            events.append({'type': 'step_finish', 'part': {'reason': 'stop'}})
            return '\n'.join(json.dumps(e) for e in events)
        parse = self.module.opencode_result
        self.assertFalse(parse(stream('read', 'File not found'), 0)['success'])
        self.assertFalse(parse(stream('read', 'Permission denied', 'read'), 0)['success'])
        self.assertFalse(parse(stream('write', 'write refused', 'read'), 0)['success'])

    def test_chuck_success_requires_zero_exit_and_consistent_typed_outcome(self):
        parse = self.module.chuck_result
        good = json.dumps({'success': True, 'outcome': 'success', 'response': 'done'})
        self.assertTrue(parse(good, 0)['success'])
        self.assertFalse(parse(good, 1)['success'])
        self.assertFalse(parse(json.dumps({'success': True, 'outcome': 'brain_error'}), 0)['success'])
        self.assertFalse(parse(json.dumps({'success': 'true', 'outcome': 'success'}), 0)['success'])

    def test_same_run_id_reuses_only_identical_inputs(self):
        calls = []
        def executor(profile, task, work, candidate, run_id):
            calls.append(run_id)
            (work / 'answer.json').write_text('{"total":"85.15"}')
            return {'success': True, 'outcome': 'success', 'response': 'done', 'usd': 0.0,
                    'exit_code': 0, 'stdout': '', 'stderr': '', 'wall_s': 0.0}
        adapter = self.module.HybridAdapter(self.root, 'chuck', executor=executor)
        first = adapter.execute('ledger-train', None, 'same')
        self.assertEqual(adapter.execute('ledger-train', None, 'same'), first)
        self.assertEqual(calls, ['same'])
        changed = self.root / 'changed'
        changed.mkdir()
        (changed / 'systemprompt.md').write_text('changed instructions')
        with self.assertRaises(ValueError):
            adapter.execute('ledger-train', changed, 'same')

    def test_cached_run_refuses_source_drift(self):
        def executor(profile, task, work, candidate, run_id):
            (work / 'answer.json').write_text('{"total":"85.15"}')
            return {'success': True, 'outcome': 'success', 'response': 'done', 'usd': 0,
                    'exit_code': 0, 'stdout': '', 'stderr': '', 'wall_s': 0}
        adapter = self.module.HybridAdapter(self.root, 'chuck', executor=executor)
        first = adapter.execute('ledger-train', None, 'same')
        (first / 'work/ledger.csv').unlink()
        with self.assertRaises(ValueError):
            adapter.execute('ledger-train', None, 'same')

    def test_false_success_remains_halted_on_cached_reuse(self):
        def executor(profile, task, work, candidate, run_id):
            (work / 'answer.json').write_text('{"total":"incorrect"}')
            return {'success': True, 'outcome': 'success', 'response': 'done', 'usd': 0,
                    'exit_code': 0, 'stdout': '', 'stderr': '', 'wall_s': 0}
        adapter = self.module.HybridAdapter(self.root, 'chuck', executor=executor)
        with self.assertRaises(ValueError):
            adapter.execute('ledger-train', None, 'bad')
        with self.assertRaises(ValueError):
            adapter.execute('ledger-train', None, 'bad')

    def test_trajectory_contains_observed_events_without_inventing_read(self):
        trace = self.module.render_trajectory('oc-v1', [
            {'type': 'tool_use', 'part': {'tool': 'write', 'state': {'status': 'completed',
                                                                    'output': 'file written'}}},
            {'type': 'text', 'part': {'text': 'done'}},
        ])
        self.assertIn('write', trace)
        self.assertNotIn('[ACTION] Read ledger.csv', trace)
        self.assertIn('file written', trace)

    def test_trajectory_never_silently_drops_later_events(self):
        adapter = self.module.HybridAdapter(self.root, 'chuck', executor=lambda *args: None)
        run = self.root / 'trace'
        run.mkdir()
        trace = '[EVENT] first\n[EVENT] terminal\n'
        (run / 'trajectory.md').write_text(trace)
        self.assertEqual(adapter.trajectory(run, obs_trunc=10), trace)

    def test_real_harness2_recursion_with_fake_workers(self):
        from harness2.core.proposer import Reply
        from harness2.core.schedule import run_domain
        from harness2.core.spec import ChannelSpec
        def executor(profile, task, work, candidate, run_id):
            (work / 'answer.json').write_text('{"total":"85.15"}')
            return {'success': True, 'outcome': 'success', 'response': 'done', 'usd': 0.0,
                    'exit_code': 0, 'stdout': '', 'stderr': '', 'wall_s': 0.0}
        adapter = self.module.HybridAdapter(self.root, 'chuck', executor=executor)
        class Proposer:
            spec = adapter.spec.channels['proposer']
            audit_dir = None
            _calls = []
            def call(self, mission, context_dir, label, **kwargs):
                workspace = Path(context_dir) / 'harness'
                reply = Reply(label=label, text='NO_ENTRY', ok=True, workspace=workspace,
                              attempts=1, channel='test-only')
                self._calls.append(reply)
                return reply
        result = run_domain('arithmetic', adapter.tasks(), 1, adapter=adapter, prop=Proposer(),
                            runs_root=adapter.spec.runs_root, library_root=self.root / 'library',
                            modes=('agg',))
        self.assertEqual((result['done'], result['failed']), (1, 0))
