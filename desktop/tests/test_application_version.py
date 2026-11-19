import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('app_builder', Path(__file__).parents[1] / 'build_app.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ApplicationVersionChecks(unittest.TestCase):
    def test_application_version_follows_project_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pyproject.toml').write_text('[project]\nversion="0.4.2"\n')
            with patch.object(builder, 'ROOT', root):
                self.assertEqual(builder.application_version(), '0.4.2')

    def test_placeholder_or_invalid_metadata_cannot_enter_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pyproject.toml').write_text('[project]\nversion="not-a-release"\n')
            with patch.object(builder, 'ROOT', root), self.assertRaisesRegex(SystemExit, 'APPLICATION_VERSION_INVALID'):
                builder.application_version()
