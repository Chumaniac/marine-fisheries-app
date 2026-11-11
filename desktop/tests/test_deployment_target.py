import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('prepare_runtime', Path(__file__).parents[1] / 'prepare_runtime.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class DeploymentTargetChecks(unittest.TestCase):
    def test_linker_version_does_not_become_the_minimum_os(self):
        self.assertEqual(module.declared_minimum("  minos 14.0\n tool LD\n version 1115.7.3\n"), "14.0")
    def test_legacy_deployment_command(self):
        self.assertEqual(module.declared_minimum("cmd LC_VERSION_MIN_MACOSX\ncmdsize 16\nversion 10.13\nsdk 13.1"), "10.13")
    def test_missing_metadata_fails_closed(self):
        with self.assertRaises(ValueError):
            module.declared_minimum("tool LD\nversion 1115.7.3")
