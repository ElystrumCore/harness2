import json
import os
import tempfile
import unittest
from pathlib import Path

from harness2.integrations.gateway import Gateway, MODEL
from harness2.integrations.hybrid import HybridAdapter, WorkerExecutor

EVIDENCE = Path(os.environ['HARNESS2_TEST_EVIDENCE_DIR']) if os.environ.get('HARNESS2_TEST_EVIDENCE_DIR') else None


def fake_provider(body, headers):
    completed = 'tool_result' in json.dumps(body['messages'])
    if completed:
        blocks = [{'type': 'text', 'text': 'The requested artifact has been written.'}]
        stop = 'end_turn'
    else:
        tools = {tool['name']: tool for tool in body.get('tools', [])}
        name = 'write_file' if 'write_file' in tools else 'write'
        schema = tools[name]['input_schema']['properties']
        field = 'filepath' if 'filepath' in schema else 'filePath'
        blocks = [{'type': 'tool_use', 'id': 'toolu_local', 'name': name,
                   'input': {field: '/work/answer.json', 'content': '{"total":"85.15"}'}}]
        stop = 'tool_use'
    message = {'id': 'msg_local_' + ('done' if completed else 'write'), 'type': 'message',
               'role': 'assistant', 'model': MODEL, 'content': blocks, 'stop_reason': stop,
               'stop_sequence': None, 'usage': {'input_tokens': 20, 'output_tokens': 30}}
    if not body.get('stream'):
        message['content'] = [{'type': 'thinking', 'thinking': 'local fixture', 'signature': 'fixture-signature'},
                              {'type': 'redacted_thinking', 'data': 'fixture-data'}] + message['content']
        return 200, {'request-id': message['id']}, json.dumps(message).encode()
    events = [{'type': 'message_start', 'message': {**message, 'content': [], 'stop_reason': None,
                                                  'usage': {'input_tokens': 20, 'output_tokens': 1}}}]
    for index, block in enumerate(blocks):
        empty = {**block, 'input': {}} if block['type'] == 'tool_use' else {**block, 'text': ''}
        events.append({'type': 'content_block_start', 'index': index, 'content_block': empty})
        delta = ({'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}
                 if block['type'] == 'tool_use' else {'type': 'text_delta', 'text': block['text']})
        events += [{'type': 'content_block_delta', 'index': index, 'delta': delta},
                   {'type': 'content_block_stop', 'index': index}]
    events += [{'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None},
                'usage': {'output_tokens': 30}}, {'type': 'message_stop'}]
    raw = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()
    return 200, {'request-id': message['id']}, raw


def executor_for(gateway):
    from harness2.integrations.runtime import load_runtime
    paths = load_runtime(os.environ['HARNESS2_NATIVE_RUNTIME'])
    return WorkerExecutor(gateway, **paths,
                          chuck_runtime=paths['chuck_python'].resolve().parent.parent)


@unittest.skipUnless(os.environ.get('HARNESS2_NATIVE_RUNTIME'), 'set HARNESS2_NATIVE_RUNTIME for pinned native worker tests')
class EndToEndTests(unittest.TestCase):
    def test_opencode_drains_large_final_events_before_cli_exit(self):
        final_text = 'terminal-drain-fixture ' * 9000

        def transport(body, headers):
            status, response_headers, raw = fake_provider(body, headers)
            if b'text_delta' in raw:
                raw = raw.replace(b'The requested artifact has been written.', final_text.encode())
            return status, response_headers, raw

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with Gateway(root / 'gateway', transport=transport) as gateway:
                adapter = HybridAdapter(root / 'bench', 'oc-v1', executor=executor_for(gateway))
                run = adapter.execute('ledger-train', None, 'mock-drain')
                row = json.loads((run / 'attempt.json').read_text())
                if EVIDENCE:
                    (EVIDENCE / 'mock-drain-outcome.json').write_text(json.dumps(row))
                self.assertTrue(row['verified'], row['outcome'])
                self.assertEqual(row['response'], final_text)
                self.assertEqual(len(gateway.ledger.done), 2)

    def test_native_workers_complete_a_tool_roundtrip_without_real_credentials(self):
        evidence = {}
        for profile in ('chuck', 'oc-v1'):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with Gateway(root / 'gateway', transport=fake_provider) as gateway:
                    adapter = HybridAdapter(root / 'bench', profile, executor=executor_for(gateway))
                    run = adapter.execute('ledger-train', None, 'mock-roundtrip')
                    row = json.loads((run / 'attempt.json').read_text())
                    evidence[profile] = row
                    if EVIDENCE:
                        (EVIDENCE / 'mock-worker-outcomes.json').write_text(json.dumps(evidence, indent=2))
                    self.assertTrue(row['verified'], {k: row[k] for k in ('outcome', 'stderr', 'stdout')})
                    self.assertEqual(len(gateway.ledger.done), 2)
                    self.assertFalse(gateway.ledger.pending)
