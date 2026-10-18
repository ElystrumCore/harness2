"""Claude API proposed these JSON fixtures; each rejection rule was reviewed."""
import json
import unittest
import tempfile
from pathlib import Path
from unittest import mock

from harness2.integrations import gateway, hybrid


class AdversarialTests(unittest.TestCase):
    def test_changed_grading_contract_invalidates_completed_attempt(self):
        def execute(profile, task, work, candidate, run_id):
            (work / 'answer.json').write_text('{"total":"85.15"}')
            return {'success': True, 'outcome': 'success', 'events': [], 'usd': 0}
        with tempfile.TemporaryDirectory() as temp:
            adapter = hybrid.HybridAdapter(Path(temp), 'chuck', executor=execute)
            adapter.execute('ledger-train', None, 'same')
            changed = {**hybrid.TASKS['ledger-train'], 'expected': 'different'}
            with mock.patch.dict(hybrid.TASKS, {'ledger-train': changed}):
                with self.assertRaises(gateway.Refused):
                    adapter.execute('ledger-train', None, 'same')

    def test_reviewed_claude_fixtures_are_all_refused(self):
        rows = json.loads((Path(__file__).parent / 'fixtures/hybrid/claude-adversarial-cases.json').read_text())
        for row in rows:
            with self.subTest(case=row['id']):
                if row['target'] == 'request':
                    with self.assertRaises(gateway.Refused):
                        gateway.validate_request(row['value'])
                elif row['target'] == 'candidate':
                    with self.assertRaises(gateway.Refused):
                        hybrid.validate_candidate(row['value'])
                else:
                    value = row['value']
                    self.assertFalse(hybrid.opencode_result('\n'.join(map(json.dumps, value['events'])), value['exit_code'])['success'])

    def test_malformed_block_fields_refuse_before_transmission(self):
        invalid = [
            ('user', {'type': 'text', 'text': 4}),
            ('assistant', {'type': 'tool_use', 'id': '', 'name': 'read', 'input': {}}),
            ('assistant', {'type': 'tool_use', 'id': 't1', 'name': 'read', 'input': []}),
            ('assistant', {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'x'}),
            ('user', {'type': 'tool_result', 'tool_use_id': 't1', 'is_error': 'false'}),
            ('user', {'type': 'tool_result', 'tool_use_id': 't1', 'content': [
                {'type': 'tool_use', 'id': 'nested', 'name': 'read', 'input': {}}]}),
            ('user', {'type': ['text'], 'text': 'untyped discriminator'}),
            (['user'], {'type': 'text', 'text': 'untyped role'}),
        ]
        for role, block in invalid:
            with self.subTest(role=role, block=block), self.assertRaises(gateway.Refused):
                gateway.validate_request({'model': gateway.MODEL, 'max_tokens': 10,
                    'messages': [{'role': role, 'content': [block]}]})

    def test_terminal_must_be_last_and_event_shapes_must_be_known(self):
        terminal = {'type': 'step_finish', 'part': {'reason': 'stop'}}
        suffixes = [
            {'type': 'step_finish', 'part': {'reason': 'tool-calls'}},
            {'type': 'text', 'part': {'text': 'a late answer'}},
            {'type': 'tool_use', 'part': 'bad'},
            {'type': 'text', 'part': {'text': {'untyped': 'data'}}},
            {'type': 'tool_use', 'part': {'tool': 'read', 'state': {
                'status': 'error', 'input': {'filePath': ['x']}, 'error': 'File not found'}}},
        ]
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                result = hybrid.opencode_result('\n'.join(map(json.dumps, [terminal, suffix])), 0)
                self.assertFalse(result['success'])
