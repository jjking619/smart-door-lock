import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

from fingerprint.service import FingerprintService
from lock.drivers import LockDriverError, PwmServoDriver, build_driver
from lock.service import LockService, Policy


class ServoDriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sysfs = self.root / "pwm"
        self.sysfs.mkdir()
        device = self.root / PwmServoDriver.CONTROLLER
        device.mkdir()
        # 故意不使用 pwmchip2，验证按控制器地址找通道。
        self.chip = self.sysfs / "pwmchip7"
        self.chip.mkdir()
        (self.chip / "device").symlink_to(device, target_is_directory=True)
        self.channel = self.chip / "pwm0"
        self._channel()
        self.driver = PwmServoDriver(sysfs=self.sysfs)
        self.sleep = patch("lock.drivers.time.sleep").start()
        self.addCleanup(patch.stopall)

    def _channel(self):
        self.channel.mkdir()
        for name in ("enable", "period", "duty_cycle", "polarity"):
            (self.channel / name).write_text("0\n", encoding="ascii")

    def test_fifty_hz_and_zero_ninety_zero_degrees(self):
        with patch.object(self.driver, "_write", wraps=self.driver._write) as write:
            self.driver.unlock(5)

        self.assertEqual((self.channel / "period").read_text().strip(), "20000000")
        duties = [c.args[1] for c in write.call_args_list if c.args[0].name == "duty_cycle"]
        self.assertEqual(duties, [0, 1_500_000, 500_000])
        self.assertEqual(self.sleep.call_args_list, [call(0.5), call(5), call(0.5)])
        self.assertEqual(self.driver.channel, self.channel)
        self.assertFalse(self.driver.unlocked)
        self.assertIsNone(self.driver.error)
        self.assertEqual((self.channel / "enable").read_text().strip(), "1")

    def test_initialization_sets_period_before_other_writes_for_new_channel(self):
        original_write = self.driver._write

        def kernel_write(path, value):
            period = int((self.channel / "period").read_text())
            if period == 0 and path.name != "period":
                raise LockDriverError("Invalid argument: period is zero")
            original_write(path, value)

        with patch.object(self.driver, "_write", side_effect=kernel_write) as write:
            self.driver._prepare()

        self.assertEqual(write.call_args_list, [
            call(self.channel / "period", 20_000_000),
            call(self.channel / "enable", 0),
            call(self.channel / "duty_cycle", 0),
            call(self.channel / "polarity", "normal"),
        ])
        self.assertTrue(self.driver._ready)

    def test_initialization_disables_and_clears_existing_duty_before_shorter_period(self):
        (self.channel / "period").write_text("40000000")
        (self.channel / "duty_cycle").write_text("30000000")
        (self.channel / "enable").write_text("1")
        (self.channel / "polarity").write_text("inversed")
        original_write = self.driver._write

        def kernel_write(path, value):
            if path.name == "period" and int((self.channel / "duty_cycle").read_text()) > value:
                raise LockDriverError("Invalid argument: duty exceeds period")
            if path.name == "polarity" and (self.channel / "enable").read_text().strip() != "0":
                raise LockDriverError("PWM still enabled")
            original_write(path, value)

        with patch.object(self.driver, "_write", side_effect=kernel_write) as write:
            self.driver._prepare()

        self.assertEqual(write.call_args_list, [
            call(self.channel / "enable", 0),
            call(self.channel / "duty_cycle", 0),
            call(self.channel / "polarity", "normal"),
            call(self.channel / "period", 20_000_000),
        ])
        self.assertTrue(self.driver._ready)

    def test_unreadable_or_invalid_period_is_reported(self):
        with patch.object(Path, "read_text", side_effect=OSError("read failed")):
            with self.assertRaisesRegex(LockDriverError, "period.*read failed"):
                self.driver._prepare()
        self.assertIn("period", self.driver.status()["error"])
        self.assertFalse(self.driver._ready)

        (self.channel / "period").write_text("not-a-number")
        with self.assertRaisesRegex(LockDriverError, "period"):
            self.driver._prepare()
        self.assertFalse(self.driver._ready)

    def test_permission_denied_tells_user_how_to_authorize(self):
        with patch("pathlib.Path.write_text", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(LockDriverError, "install_pwm_access.sh"):
                self.driver.unlock(5)
        self.assertIn("install_pwm_access.sh", self.driver.status()["error"])

    def test_calibration_and_reversed_angles(self):
        driver = PwmServoDriver(
            sysfs=self.sysfs, min_us=1000, max_us=2000,
            locked_angle=90, unlocked_angle=0,
        )
        self.assertEqual(driver._duty(0), 1_000_000)
        self.assertEqual(driver._duty(90), 1_500_000)
        self.assertEqual(driver._duty(180), 2_000_000)
        driver.unlock(5)
        self.assertEqual((self.channel / "duty_cycle").read_text().strip(), "1500000")

    def test_export_uses_channel_zero_not_soc_channel_three(self):
        for path in self.channel.iterdir():
            path.unlink()
        self.channel.rmdir()
        original = self.driver._write

        def write(path, value):
            if path.name == "export":
                self.assertEqual(value, 0)
                self._channel()
            else:
                original(path, value)

        with patch.object(self.driver, "_write", side_effect=write):
            self.driver.lock()
        self.assertFalse(self.driver.unlocked)

    def test_close_moves_to_lock_before_disabling_pwm(self):
        with patch.object(self.driver, "_write", wraps=self.driver._write) as write:
            self.driver.close()
        self.assertEqual(write.call_args_list[-1], call(self.channel / "enable", 0))
        self.assertEqual((self.channel / "duty_cycle").read_text().strip(), "500000")

    def test_missing_controller_is_an_error_not_a_mock_fallback(self):
        driver = PwmServoDriver(sysfs=self.root / "missing")
        with self.assertRaisesRegex(LockDriverError, "2ade3000"):
            driver.lock()

    def test_permission_error_has_the_failing_attribute(self):
        (self.channel / "period").write_text("20000000")
        with patch.object(Path, "write_text", side_effect=PermissionError("Permission denied")):
            with self.assertRaisesRegex(LockDriverError, "enable.*Permission denied"):
                self.driver.lock()

    def test_open_failure_still_attempts_relock(self):
        with patch.object(self.driver, "_move", side_effect=[LockDriverError("开锁失败"), None]) as move:
            with self.assertRaisesRegex(LockDriverError, "开锁失败"):
                self.driver.unlock(5)
        self.assertEqual(move.call_args_list, [call(90), call(0)])
        self.assertFalse(self.driver.unlocked)
        self.assertEqual(self.driver.error, "开锁失败")

    def test_return_failure_propagates_and_does_not_claim_locked(self):
        with patch.object(self.driver, "_move", side_effect=[None, LockDriverError("回锁失败")]):
            with self.assertRaisesRegex(LockDriverError, "回锁失败"):
                self.driver.unlock(5)
        self.assertTrue(self.driver.unlocked)
        self.assertEqual(self.driver.error, "回锁失败")
        service = LockService(self.root, self.driver, snapshot_url="")
        self.assertEqual(service.status()["state"], "fault")

    def test_invalid_configuration_is_rejected(self):
        for parameters in (
            {"locked_angle": -1}, {"unlocked_angle": 181},
            {"locked_angle": 90}, {"min_us": 2500, "max_us": 500},
            {"max_us": 20_000}, {"move_s": 0}, {"min_us": float("nan")},
        ):
            with self.subTest(parameters=parameters), self.assertRaises(LockDriverError):
                PwmServoDriver(**parameters)
        for seconds in (0, -1, float("nan"), float("inf")):
            with self.subTest(seconds=seconds), self.assertRaises(LockDriverError):
                self.driver.unlock(seconds)

    def test_factory_selects_servo_and_refuses_typo(self):
        options = dict(pulse=5, chip="gpiochip0", line=0, active_low=False)
        self.assertIsInstance(build_driver("servo", **options), PwmServoDriver)
        with self.assertRaisesRegex(LockDriverError, "未知"):
            build_driver("serv0", **options)

    def test_registered_fingerprint_and_panel_share_the_servo_driver(self):
        data = self.root / "data"
        (data / "fingerprint").mkdir(parents=True)
        (data / "fingerprint" / "users.json").write_text(json.dumps([
            {"id": 0, "name": "kane", "finger": ""},
        ]))
        service = LockService(data, self.driver, Policy(), snapshot_url="")
        with patch.object(self.driver, "_move", wraps=self.driver._move) as move:
            rejected = service.request_unlock(user_id=0, score=20)
            self.assertFalse(rejected["ok"])
            move.assert_not_called()
            self.assertTrue(service.request_unlock(user_id=0, score=90)["ok"])
            self.assertTrue(service.request_unlock(source="ui")["ok"])
        self.assertEqual(move.call_args_list, [call(90), call(0), call(90), call(0)])

    def test_fingerprint_scan_success_triggers_servo_and_reports_result(self):
        from unittest.mock import Mock

        data = self.root / "data"
        (data / "fingerprint").mkdir(parents=True)
        (data / "fingerprint" / "users.json").write_text(json.dumps([
            {"id": 0, "name": "kane", "finger": ""},
        ]))
        lock = LockService(data, self.driver, Policy(), snapshot_url="")
        fingerprint = FingerprintService(
            data, unlock_notifier=lambda user_id, name, score: lock.request_unlock(
                user_id=user_id, name=name, score=score,
            ),
        )
        fingerprint._sensor = Mock(
            finger_id=0, confidence=90,
            get_image=Mock(return_value=0),
            image_2_tz=Mock(return_value=0),
            finger_fast_search=Mock(return_value=0),
        )
        with patch.object(fingerprint, "_wait_release"):
            with patch.object(self.driver, "_move", wraps=self.driver._move) as move:
                fingerprint._scan_once()
        self.assertEqual(move.call_args_list, [call(90), call(0)])
        self.assertTrue(fingerprint.status()["last_unlock"]["ok"])
        self.assertEqual(lock.status()["unlock_count"], 1)


if __name__ == "__main__":
    unittest.main()
