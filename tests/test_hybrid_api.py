import importlib
import json
import tempfile
import unittest
from pathlib import Path

from harness2.core.proposer import Proposer, ProposerError
from harness2.core.spec import ChannelSpec, resolve_channel_model
from harness2.integrations.gateway import Gateway, MODEL


class ApiTests(unittest.TestCase):
    def test_direct_channel_cannot_fall_back_to_opencode_or_vertex(self):
        spec = resolve_channel_model('anthropic_api', MODEL)
        self.assertEqual(spec.kind, 'anthropic_api')
        with self.assertRaises(ValueError):
            resolve_channel_model('anthropic_api', 'anthropic/' + MODEL)
        with self.assertRaises(ValueError):
            Proposer(spec)

    def test_api_proposals_are_data_and_cannot_add_executable_files(self):
        module = importlib.import_module('harness2.integrations.anthropic_channel')
        def provider(body, headers):
            text = json.dumps({'files': {'scripts/evil.py': 'print(1)'}, 'text': 'done'})
            return 200, {'request-id': 'req_api'}, json.dumps({
                'id': 'msg_api', 'model': MODEL, 'stop_reason': 'end_turn',
                'usage': {'input_tokens': 30, 'output_tokens': 20},
                'content': [{'type': 'text', 'text': text}]}).encode()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / 'context/harness'
            context.mkdir(parents=True)
            (context / 'systemprompt.md').write_text('base')
            with Gateway(root / 'gateway', transport=provider) as gateway:
                proposer = module.HybridApiProposer(gateway, root / 'audit')
                with self.assertRaises(ProposerError):
                    proposer.call('Improve guidance', context.parent, 'inv1.a1', harvest='context/harness')
                self.assertGreater(gateway.ledger.spent, 0)
                self.assertFalse((context / 'scripts/evil.py').exists())

    def test_valid_proposal_uses_real_gateway_receipt_and_staged_workspace(self):
        module = importlib.import_module('harness2.integrations.anthropic_channel')
        def provider(body, headers):
            text = json.dumps({'files': {'systemprompt.md': 'Inspect signed values before writing.'},
                               'text': '<notes>Evidence-driven change.</notes>'})
            return 200, {'request-id': 'req_ok'}, json.dumps({
                'id': 'msg_ok', 'model': MODEL, 'stop_reason': 'end_turn',
                'usage': {'input_tokens': 30, 'output_tokens': 20},
                'content': [{'type': 'text', 'text': text}]}).encode()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / 'context/harness'
            context.mkdir(parents=True)
            (context / 'systemprompt.md').write_text('base')
            with Gateway(root / 'gateway', transport=provider) as gateway:
                proposer = module.HybridApiProposer(gateway, root / 'audit')
                reply = proposer.call('Improve guidance', context.parent, 'inv1.a1', harvest='context/harness')
                self.assertTrue(reply.ok)
                self.assertGreater(reply.cost_usd, 0)
                self.assertEqual(reply.channel, 'anthropic_api')
                self.assertEqual((reply.workspace / 'systemprompt.md').read_text(), 'Inspect signed values before writing.')
                self.assertEqual((context / 'systemprompt.md').read_text(), 'base')
