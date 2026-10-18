import importlib
import json
import tempfile
import unittest
from pathlib import Path


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('harness2.integrations.gateway')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'receipts.jsonl'
        self.ledger = self.module.Ledger(self.path, total_cap=0.02, session_cap=0.01)

    def test_reserves_before_send_and_counts_settlement_once(self):
        reservation = self.ledger.reserve('session', 'request', 0.009)
        self.ledger.settle(reservation, {'request_id': 'provider-1', 'usd': 0.003})
        self.ledger.settle(reservation, {'request_id': 'provider-1', 'usd': 0.003})
        self.assertAlmostEqual(self.ledger.spent, 0.003)
        self.assertEqual(len(self.ledger.pending), 0)
        self.assertEqual(len(self.path.read_text().splitlines()), 2)

    def test_unresolved_request_blocks_restart_and_more_spend(self):
        self.ledger.reserve('s', 'one', 0.008)
        with self.assertRaises(self.module.Refused):
            self.ledger.reserve('s', 'two', 0.008)
        with self.assertRaises(self.module.Refused):
            self.module.Ledger(self.path, total_cap=0.02, session_cap=0.01)

    def test_second_controller_cannot_open_same_ledger(self):
        with self.assertRaises(self.module.Refused):
            self.module.Ledger(self.path, total_cap=0.02, session_cap=0.01)

    def test_provider_receipt_cannot_be_counted_under_two_reservations(self):
        r = self.ledger.reserve('s', 'one', 0.005)
        self.ledger.settle(r, {'request_id': 'same-provider-response', 'usd': 0.001})
        r = self.ledger.reserve('s', 'two', 0.005)
        with self.assertRaises(self.module.Refused):
            self.ledger.settle(r, {'request_id': 'same-provider-response', 'usd': 0.001})

    def test_session_cap_and_global_cap(self):
        r = self.ledger.reserve('s1', 'one', 0.009)
        self.ledger.settle(r, {'request_id': 'a', 'usd': 0.009})
        with self.assertRaises(self.module.Refused):
            self.ledger.reserve('s1', 'two', 0.002)
        r = self.ledger.reserve('s2', 'two', 0.009)
        self.ledger.settle(r, {'request_id': 'b', 'usd': 0.009})
        with self.assertRaises(self.module.Refused):
            self.ledger.reserve('s3', 'three', 0.003)

    def test_duplicate_identity_and_changed_receipt_refuse(self):
        r = self.ledger.reserve('s', 'r', 0.008)
        self.ledger.settle(r, {'request_id': 'a', 'usd': 0.002})
        with self.assertRaises(self.module.Refused):
            self.ledger.reserve('s', 'r', 0.008)
        with self.assertRaises(self.module.Refused):
            self.ledger.settle(r, {'request_id': 'other', 'usd': 0.002})

    def test_nonfinite_and_over_reservation_receipt_refuse(self):
        for value in (float('nan'), float('inf'), -1, True, 0):
            with self.assertRaises(self.module.Refused):
                self.ledger.reserve('s', 'r', value)
        r = self.ledger.reserve('s', 'r', 0.008)
        with self.assertRaises(self.module.Refused):
            self.ledger.settle(r, {'request_id': 'a', 'usd': 0.009})
        self.assertIn(r, self.ledger.pending)

    def test_missing_or_invalid_usage_is_never_free(self):
        for usage in ({}, {'input_tokens': 1}, {'input_tokens': True, 'output_tokens': 4}):
            with self.assertRaises(self.module.Refused):
                self.module.usage_cost(usage)
        usage = {'input_tokens': 100, 'output_tokens': 100, 'cache_read_input_tokens': 100,
                 'cache_creation_input_tokens': 100}
        self.assertAlmostEqual(self.module.usage_cost(usage), 0.0000735)

    def test_only_bounded_haiku_text_requests_allowed(self):
        body = {'model': self.module.MODEL, 'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 512}
        self.assertGreater(self.module.validate_request(body), 0)
        for update in ({'model': 'unapproved'}, {'max_tokens': 100000}, {'max_tokens': True},
                       {'service_tier': 'priority'}, {'tools': [{'type': 'web_search_20250305'}]},
                       {'messages': [{'role': 'user', 'content': [{'type': 'image', 'source': {}}]}]}):
            with self.assertRaises(self.module.Refused):
                self.module.validate_request({**body, **update})

    def test_stream_requires_terminal_and_aggregates_usage_without_duplicate_input(self):
        events = [
            {'type': 'message_start', 'message': {'id': 'msg_1', 'model': self.module.MODEL,
             'usage': {'input_tokens': 20, 'output_tokens': 1}}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 12}},
            {'type': 'message_stop'},
        ]
        raw = ''.join('data: ' + json.dumps(v) + '\n\n' for v in events).encode()
        parsed = self.module.response_usage(raw, streaming=True)
        self.assertEqual(parsed['usage'], {'input_tokens': 20, 'output_tokens': 12})
        with self.assertRaises(self.module.Refused):
            self.module.response_usage(raw.rsplit(b'data:', 1)[0], streaming=True)

    def test_signed_assistant_thinking_is_preserved_for_tool_result_replay(self):
        body = {'model': self.module.MODEL, 'max_tokens': 512, 'messages': [
            {'role': 'user', 'content': 'Read a file'},
            {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': 'prior reasoning', 'signature': 'opaque-signature'},
                {'type': 'redacted_thinking', 'data': 'opaque-data'},
                {'type': 'tool_use', 'id': 't1', 'name': 'read_file', 'input': {'path': 'x'}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'data'}]},
        ]}
        before = json.dumps(body, sort_keys=True)
        self.assertGreater(self.module.validate_request(body), 0)
        self.assertEqual(json.dumps(body, sort_keys=True), before)
