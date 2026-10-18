import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get('HARNESS2_NATIVE_TESTS') == '1', 'set HARNESS2_NATIVE_TESTS=1 for actual namespace tests')
class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('harness2.integrations.worker')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_actual_namespace_blocks_host_files_and_network(self):
        secret = self.root / 'outside-worker.txt'
        secret.write_text('not visible')
        task = self.root / 'work'
        task.mkdir()
        source = '''import json, os, socket
from pathlib import Path
checks = {'outside_absent': not Path(%r).exists(),
          'home_absent': not Path('/home/worker/.hermes').exists(),
          'grader_code_absent': not Path('/bridge/hybrid.py').exists(),
          'real_key_absent': 'HOST_SECRET_FOR_TEST' not in os.environ}
try:
    socket.create_connection(('1.1.1.1', 443), timeout=.2)
    checks['network_blocked'] = False
except OSError:
    checks['network_blocked'] = True
print(json.dumps(checks))
''' % str(secret)
        (task / 'probe.py').write_text(source)
        result = self.module.run_isolated(['/usr/bin/python3', '/work/probe.py'], task,
                                          environment={'HOST_SECRET_FOR_TEST': 'not forwarded'})
        self.assertEqual(result['exit_code'], 0, result)
        self.assertTrue(all(json.loads(result['stdout']).values()), result)

    def test_timeout_and_output_flood_are_bounded(self):
        task = self.root / 'work'
        task.mkdir()
        result = self.module.run_isolated(['/usr/bin/python3', '-c', 'import time; time.sleep(30)'],
                                          task, timeout=.3)
        self.assertEqual(result['outcome'], 'timeout')
        result = self.module.run_isolated(['/usr/bin/python3', '-c', 'print("x"*3000000)'], task,
                                          max_output=10000)
        self.assertEqual(result['outcome'], 'output_limit')
        self.assertLessEqual(len(result['stdout']), 10000)

    def test_private_unix_gateway_is_only_paid_route(self):
        gateway_module = importlib.import_module('harness2.integrations.gateway')
        def fake(body, headers):
            raw = json.dumps({'id': 'msg_test', 'model': gateway_module.MODEL,
                              'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'ok'}],
                              'usage': {'input_tokens': 5, 'output_tokens': 2}}).encode()
            return 200, {'request-id': 'req_test'}, raw
        task = self.root / 'work'
        task.mkdir()
        (task / 'probe.py').write_text('''import os, json, urllib.request
req=urllib.request.Request(os.environ['ANTHROPIC_BASE_URL']+'/v1/messages',
 data=json.dumps({'model': 'claude-haiku-5-5', 'max_tokens': 30,
 'messages':[{'role':'user','content':'hello'}]}).encode(),
 headers={'Content-Type':'application/json','x-api-key':os.environ['ANTHROPIC_API_KEY']})
print(urllib.request.urlopen(req).read().decode())
''')
        with gateway_module.Gateway(self.root / 'gateway', transport=fake) as gateway:
            token = gateway.token('probe')
            result = self.module.run_isolated(['/usr/bin/python3', '/work/probe.py'], task,
                                              gateway=gateway, token=token)
            self.assertEqual(result['exit_code'], 0, result)
            self.assertEqual(json.loads(result['stdout'])['id'], 'msg_test')
            self.assertGreater(gateway.ledger.spent, 0)
            self.assertFalse(gateway.ledger.pending)
