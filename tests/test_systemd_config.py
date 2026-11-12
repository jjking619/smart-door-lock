import configparser
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from audio.coze_setup import write_env
from deploy.generate_systemd import generate


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "systemd"


class SystemdConfigTests(unittest.TestCase):
    def test_generates_services_for_actual_project_and_preserves_existing_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project $name % "quoted"'
            deploy = root / "deploy" / "systemd"
            shutil.copytree(DEPLOY, deploy, ignore=shutil.ignore_patterns("doorlock.env", "generated"))
            env = deploy / "doorlock.env"
            env.write_text("COZE_API_TOKEN=existing-secret\nVISION_DATA_DIR=custom\n")
            env.chmod(0o640)

            generated = generate(root, "actualuser")

            self.assertEqual(env.read_text(), "COZE_API_TOKEN=existing-secret\nVISION_DATA_DIR=custom\n")
            self.assertEqual(env.stat().st_mode & 0o777, 0o640)
            for name in ("visiond", "audiood", "fingerprintd", "lockd"):
                unit = configparser.ConfigParser(interpolation=None)
                unit.read(generated / f"{name}.service")
                service = unit["Service"]
                self.assertEqual(service["User"], "actualuser")
                self.assertEqual(service["WorkingDirectory"].replace("%%", "%"), str(root))
                self.assertEqual(service["EnvironmentFile"].replace("%%", "%"), str(env))
                arguments = shlex.split(service["ExecStart"])
                self.assertEqual(arguments[0], "/usr/bin/env")
                self.assertEqual(arguments[1].replace("%%", "%").replace("$$", "$"), str(root / ".venv/bin/python"))
                self.assertEqual(arguments[2].replace("%%", "%").replace("$$", "$"), str(root / "main.py"))

    def test_creates_private_environment_only_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deploy = root / "deploy" / "systemd"
            shutil.copytree(DEPLOY, deploy, ignore=shutil.ignore_patterns("doorlock.env", "generated"))
            generate(root, "actualuser")
            env = deploy / "doorlock.env"
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            self.assertIn("VISION_DATA_DIR=data\n", env.read_text())
            self.assertIn("FINGERPRINT_DATA_DIR=data\n", env.read_text())
            env.write_text("COZE_API_TOKEN=keep-me\n")
            generate(root, "actualuser")
            self.assertEqual(env.read_text(), "COZE_API_TOKEN=keep-me\n")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is unavailable")
    def test_systemd_accepts_generated_units_with_special_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project $name % "quoted"'
            deploy = root / "deploy" / "systemd"
            shutil.copytree(DEPLOY, deploy, ignore=shutil.ignore_patterns("doorlock.env", "generated"))
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nexit 0\n")
            python.chmod(0o755)
            generated = generate(root, "actualuser")
            result = subprocess.run(
                ["systemd-analyze", "verify", *(str(path) for path in sorted(generated.glob("*.service")))],
                text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(str(generated), result.stderr)

    def test_services_share_required_environment_file(self):
        for name in ("visiond", "audiood", "fingerprintd", "lockd"):
            with self.subTest(service=name):
                unit = configparser.ConfigParser(interpolation=None)
                unit.read(DEPLOY / f"{name}.service")
                self.assertEqual(
                    unit["Service"]["EnvironmentFile"],
                    "@PROJECT_ROOT@/deploy/systemd/doorlock.env",
                )

    def test_only_one_environment_template(self):
        self.assertEqual(
            sorted(p.name for p in DEPLOY.glob("*.env.example")),
            ["doorlock.env.example"],
        )

    def test_template_has_unique_keys_and_is_shell_compatible(self):
        text = (DEPLOY / "doorlock.env.example").read_text()
        expected = {}
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            key, raw = line.split("=", 1)
            self.assertNotIn(key, expected, key)
            values = shlex.split(raw)
            self.assertLessEqual(len(values), 1, key)
            expected[key] = values[0] if values else ""
        command = [
            "bash", "-c",
            'set -ae; . "$1"; env -0',
            "bash", str(DEPLOY / "doorlock.env.example"),
        ]
        result = subprocess.run(command, env={"PATH": os.defpath}, capture_output=True, check=True)
        actual = dict(item.split(b"=", 1) for item in result.stdout.split(b"\0") if b"=" in item)
        for key, value in expected.items():
            self.assertEqual(actual[key.encode()].decode(), value, key)
        self.assertEqual(expected["VISION_HTTP_PORT"], "8083")
        self.assertEqual(expected["COZE_BOT_TIMEOUT_S"], "15")
        self.assertEqual(expected["MQTT_HOST"], "")

    def test_bot_setup_preserves_shared_settings_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doorlock.env"
            original = (DEPLOY / "doorlock.env.example").read_text()
            path.write_text(original)
            path.chmod(0o600)
            write_env(path, "12345")
            self.assertEqual(
                path.read_text(),
                original.replace("COZE_BOT_ID=7466xxxxxxxxxxxxxxxx", "COZE_BOT_ID=12345"),
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
