import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / "install.sh", self.root)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.log"
        self._command("uname", 'echo aarch64')
        self._command("id", 'echo 1000')
        self._command("sudo", 'echo "sudo $*" >> "$INSTALL_TEST_LOG"')
        self._command("apt-get", 'exit 0')
        self._command(
            "uv",
            'echo "uv $*" >> "$INSTALL_TEST_LOG"\n'
            'if [ "${INSTALL_TEST_FAIL_UV:-0}" = 1 ]; then exit 7; fi\n'
            'if [ "$1" = venv ]; then\n'
            '  mkdir -p "$4/bin"\n'
            '  printf "#!/bin/sh\\nexit 0\\n" > "$4/bin/python"\n'
            '  chmod +x "$4/bin/python"\n'
            'fi',
        )
        for env in (".venv", ".venv-ui"):
            python = self.root / env / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nexit 0\n")
            python.chmod(0o755)
        models = self.root / "models"
        models.mkdir()
        (models / "yolov5s-rk3576.rknn").write_bytes(b"existing-model")
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}:/usr/bin:/bin"
        self.env["INSTALL_TEST_LOG"] = str(self.log)

    def _command(self, name, content):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{content}\n")
        path.chmod(0o755)

    def _run(self):
        return subprocess.run(
            ["bash", str(self.root / "install.sh")],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def test_missing_parcel_reports_single_model_command(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Deployment complete.", result.stdout)
        self.assertIn('VISION_PACKAGE_MODEL="" LOCK_DRIVER=mock', result.stdout)
        self.assertEqual(
            (self.root / "models" / "yolov5s-rk3576.rknn").read_bytes(),
            b"existing-model",
        )
        commands = self.log.read_text()
        self.assertIn("-e .", commands)
        self.assertIn("-r requirements.txt", commands)
        self.assertIn("rknn_toolkit_lite2-2.3.2", commands)

    def test_existing_parcel_enables_normal_command_and_rerun(self):
        (self.root / "models" / "parcel-yolov5n-rk3576-fp.rknn").write_bytes(b"parcel")
        for _ in range(2):
            result = self._run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("启动：LOCK_DRIVER=mock", result.stdout)
            self.assertNotIn("缺少 models/parcel", result.stdout)

    def test_dependency_failure_does_not_report_success(self):
        self.env["INSTALL_TEST_FAIL_UV"] = "1"
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Deployment complete.", result.stdout)

    def test_creates_missing_backend_environment(self):
        shutil.rmtree(self.root / ".venv")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uv venv --python 3.11 .venv", self.log.read_text())
        self.assertTrue((self.root / ".venv" / "bin" / "python").is_file())

    def test_unsupported_architecture_fails_before_installing(self):
        self._command("uname", "echo x86_64")
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ARM64", result.stderr)
        self.assertFalse(self.log.exists())

    def test_broken_existing_environment_is_not_replaced(self):
        (self.root / ".venv" / "bin" / "python").unlink()
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".venv 已存在", result.stderr)
        self.assertNotIn("Deployment complete.", result.stdout)


if __name__ == "__main__":
    unittest.main()
