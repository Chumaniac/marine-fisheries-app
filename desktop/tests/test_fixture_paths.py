"""Process checks for the fixed, caller-owned native fixture output directory."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

FIXTURE = Path(__file__).with_name("backend_fixture.py")


class FixturePathChecks(unittest.TestCase):
    def setUp(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.root = Path(tempfile.gettempdir()).resolve() / ("native-fixture-" + str(self.port))
        self.assertFalse(self.root.exists() or self.root.is_symlink())

    def refuse(self, *extra):
        result = subprocess.run([sys.executable, str(FIXTURE), str(self.port), "normal", *extra],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        self.assertNotEqual(result.returncode, 0)

    def test_output_path_argument_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="fixture-path-refusal-") as directory:
            output = Path(directory) / "outside.json"
            self.refuse(str(output))
            self.assertFalse(output.exists())

    def test_existing_file_is_preserved(self):
        self.root.mkdir(mode=0o700)
        output = self.root / "result.json"
        try:
            output.write_text("retained synthetic fixture")
            self.refuse()
            self.assertEqual(output.read_text(), "retained synthetic fixture")
        finally:
            output.unlink(missing_ok=True)
            self.root.rmdir()

    def test_symlink_file_is_preserved(self):
        self.root.mkdir(mode=0o700)
        link = self.root / "result.json"
        try:
            with tempfile.TemporaryDirectory(prefix="fixture-file-target-") as directory:
                target = Path(directory) / "retained.txt"
                target.write_text("retained synthetic fixture")
                link.symlink_to(target)
                self.refuse()
                self.assertTrue(link.is_symlink())
                self.assertEqual(target.read_text(), "retained synthetic fixture")
        finally:
            link.unlink(missing_ok=True)
            self.root.rmdir()

    def test_symlink_directory_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="fixture-link-target-") as target:
            self.root.symlink_to(target, target_is_directory=True)
            try:
                self.refuse()
                self.assertEqual(list(Path(target).iterdir()), [])
            finally:
                self.root.unlink()

    def test_shared_directory_is_refused(self):
        self.root.mkdir(mode=0o700)
        try:
            os.chmod(self.root, 0o777)
            self.refuse()
            self.assertFalse((self.root / "result.json").exists())
        finally:
            os.chmod(self.root, 0o700)
            self.root.rmdir()


if __name__ == "__main__":
    unittest.main()
