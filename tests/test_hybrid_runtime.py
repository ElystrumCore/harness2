"""Portable configuration contracts; no native runtimes or credentials required."""
import importlib
import json
import tempfile
import unittest
from pathlib import Path


class RuntimeTests(unittest.TestCase):
    def module(self):
        return importlib.import_module('harness2.integrations.runtime')

    def test_missing_runtime_paths_refuse_before_loading_credentials(self):
        runtime = self.module()
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'runtime.json'
            config.write_text(json.dumps({'chuck_root': '/missing'}))
            with self.assertRaises(ValueError):
                runtime.load_runtime(config)

    def test_explicit_existing_runtime_paths_are_loaded_without_host_defaults(self):
        runtime = self.module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            values = {}
            for name in ('oc_root', 'bun', 'chuck_root', 'hub_root', 'chuck_python'):
                path = root / name
                path.mkdir() if name.endswith('_root') else path.write_text('fixture')
                values[name] = str(path)
            config = root / 'runtime.json'
            config.write_text(json.dumps(values))
            self.assertEqual(runtime.load_runtime(config), {k: Path(v) for k, v in values.items()})
            values['provider_key'] = 'not allowed'
            config.write_text(json.dumps(values))
            with self.assertRaises(ValueError):
                runtime.load_runtime(config)

    def test_offline_cli_plan_needs_no_runtime_config(self):
        import contextlib
        import io
        from harness2.integrations import pilot
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(pilot.main(['--plan']), 0)
        self.assertEqual(json.loads(output.getvalue())['memory'], 'off')
