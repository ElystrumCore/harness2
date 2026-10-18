import importlib
import unittest
import tempfile
from pathlib import Path
from harness2.integrations.gateway import Ledger, Refused


class PilotTests(unittest.TestCase):
    def test_global_plan_has_bounded_explicit_roles_and_no_live_side_effects(self):
        module = importlib.import_module('harness2.integrations.pilot')
        plan = module.plan()
        self.assertEqual(plan['total_cap_usd'], 5.0)
        self.assertLessEqual(plan['maximum_session_reservations_usd'], 5.0)
        self.assertEqual(plan['mode'], 'agg')
        self.assertEqual(plan['k'], 1)
        self.assertEqual(plan['concurrency'], 1)
        self.assertEqual(plan['training_tasks'], ['ledger-train'])
        self.assertEqual(plan['heldout_tasks'], ['ledger-heldout'])
        self.assertEqual(plan['baseline_profiles'], ['chuck', 'oc-v1'])

    def test_previous_experiments_consume_the_same_five_dollar_budget(self):
        module = importlib.import_module('harness2.integrations.pilot')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger = Ledger(root / 'run-001/gateway/receipts.jsonl')
            rid = ledger.reserve('session', 'request', .1)
            ledger.settle(rid, {'request_id': 'req_prior', 'usd': .02})
            ledger.close()
            self.assertAlmostEqual(module.prior_spend(root), .02)
            ledger = Ledger(root / 'run-002/gateway/receipts.jsonl')
            ledger.reserve('session', 'request', .1)
            ledger.close()
            with self.assertRaises(Refused):
                module.prior_spend(root)

    def test_series_retains_false_success_halt_across_new_run_directories(self):
        module = importlib.import_module('harness2.integrations.pilot')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / 'run-001/gateway/STOPPED.json'
            marker.parent.mkdir(parents=True)
            marker.write_text('{"reason":"false_success"}')
            with self.assertRaises(Refused):
                module.prior_spend(root)
