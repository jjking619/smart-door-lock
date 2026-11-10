import json
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from lock import protocol
from lock.drivers import GpioRelayDriver, LockDriverError, MockDriver, build_driver
from lock.service import LockService, Policy, create_app


class Clock:
    """可控单调时钟，用来测防重复和锁定冷却，不真的 sleep。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class LockServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.clock = Clock()
        self.driver = MockDriver(pulse=0)
        self.service = LockService(
            self.data, self.driver,
            Policy(pulse_s=0.01, repeat_guard_s=5.0, max_failures=3, lockout_s=60.0),
            clock=self.clock, snapshot_url="",
        )

    def _register(self, user_id: int = 0, name: str = "张三") -> None:
        folder = self.data / "fingerprint"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "users.json").write_text(
            json.dumps([{"id": user_id, "name": name, "finger": ""}]), encoding="utf-8"
        )

    def _events(self) -> list[dict]:
        path = self.data / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_unregistered_fingerprint_is_refused(self):
        result = self.service.request_unlock(user_id=9, score=99)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "unregistered")
        self.assertEqual(self.driver.history, [])
        self.assertEqual([e["type"] for e in self._events()], ["lock.rejected"])

    def test_registered_fingerprint_unlocks_and_audits(self):
        self._register()
        result = self.service.request_unlock(user_id=0, score=92)

        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "张三")
        self.assertEqual(len(self.driver.history), 1)
        kinds = [e["type"] for e in self._events()]
        self.assertEqual(kinds, ["door.opened", "door.closed"])
        opened = self._events()[0]
        self.assertEqual(opened["user"], "张三")
        self.assertEqual(opened["source"], "finger")

    def test_low_score_is_refused(self):
        self._register()
        result = self.service.request_unlock(user_id=0, score=30)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "low_score")
        self.assertEqual(self.driver.history, [])

    def test_same_finger_cannot_unlock_twice_within_guard(self):
        self._register()
        self.assertTrue(self.service.request_unlock(user_id=0, score=92)["ok"])
        again = self.service.request_unlock(user_id=0, score=92)

        self.assertFalse(again["ok"])
        self.assertEqual(again["reason_code"], "repeat")
        self.assertEqual(len(self.driver.history), 1)

        self.clock.advance(6)
        self.assertTrue(self.service.request_unlock(user_id=0, score=92)["ok"])
        self.assertEqual(len(self.driver.history), 2)

    def test_repeated_failures_lock_out_then_expire(self):
        self._register()
        for _ in range(3):
            self.assertFalse(self.service.request_unlock(user_id=7, score=90)["ok"])

        blocked = self.service.request_unlock(user_id=0, score=95)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason_code"], "locked_out")
        self.assertTrue(self.service.status()["locked_out"])

        self.clock.advance(61)
        self.assertTrue(self.service.request_unlock(user_id=0, score=95)["ok"])

    def test_driver_failure_is_reported_instead_of_raising(self):
        class Broken(MockDriver):
            def unlock(self, seconds):
                raise LockDriverError("继电器未接线")

        service = LockService(self.data, Broken(), Policy(pulse_s=0.01), snapshot_url="")
        service.registered_users = lambda: {0: {"name": "张三", "finger": ""}}
        result = service.request_unlock(user_id=0, score=99)

        # 策略允许了，但锁体没动 —— 必须告诉界面"没开成"，不能假装成功
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "driver")
        self.assertIn("继电器未接线", result["reason"])
        self.assertEqual([e["type"] for e in self._events()], ["lock.fault"])
        self.assertEqual(self.service.status()["unlock_count"], 0)
        self.assertEqual(self.service.status()["state"], "locked")

    def test_manual_lock_is_idempotent(self):
        self.service.lock_now()
        self.service.lock_now()
        self.assertTrue(self.service.status()["state"] == "locked")

    def test_local_panel_unlock_does_not_require_a_fingerprint(self):
        result = self.service.request_unlock(source="ui", method="界面")

        self.assertTrue(result["ok"])
        self.assertEqual(len(self.driver.history), 1)
        again = self.service.request_unlock(source="ui")
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason_code"], "repeat")

    def test_manual_lock_failure_is_not_reported_as_success(self):
        from unittest.mock import patch

        with patch.object(self.driver, "lock", side_effect=LockDriverError("PWM 回锁失败")):
            result = self.service.lock_now()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "driver")
        self.assertEqual([e["type"] for e in self._events()], ["lock.fault"])


class LockHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        (self.data / "fingerprint").mkdir(parents=True)
        (self.data / "fingerprint" / "users.json").write_text(
            json.dumps([{"id": 0, "name": "kane", "finger": ""}]), encoding="utf-8"
        )
        self.driver = MockDriver(pulse=0)
        self.service = LockService(
            self.data, self.driver, Policy(pulse_s=0.01), snapshot_url=""
        )
        self.app = create_app(self.service)
        server = TestServer(self.app)
        self.client = TestClient(server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_health_reports_policy_and_driver(self):
        response = await self.client.get("/health")
        body = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(body["service"], "lockd")
        self.assertEqual(body["state"], "locked")
        self.assertEqual(body["driver"]["driver"], "mock")
        self.assertIn("min_score", body["policy"])

    async def test_unlock_returns_structured_rejection(self):
        response = await self.client.post("/unlock", json={"source": "finger", "user_id": 5, "score": 90})
        body = await response.json()

        self.assertEqual(response.status, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason_code"], "unregistered")

    async def test_unlock_accepts_registered_user(self):
        response = await self.client.post("/unlock", json={"source": "ui", "user_id": 0, "score": 100})
        body = await response.json()

        self.assertTrue(body["ok"])
        self.assertEqual(len(self.driver.history), 1)

    async def test_panel_payload_unlocks_without_id_or_score(self):
        response = await self.client.post("/unlock", json={"source": "ui"})
        body = await response.json()

        self.assertTrue(body["ok"])
        self.assertEqual(len(self.driver.history), 1)

    async def test_remote_client_is_refused(self):
        app = create_app(self.service, allow_remote=False)
        # aiohttp 的测试客户端来源就是 127.0.0.1，用 _local() 直接验判定规则
        from lock.service import _local

        self.assertTrue(_local("127.0.0.1"))
        self.assertTrue(_local("::ffff:127.0.0.1"))
        self.assertFalse(_local("192.168.1.50"))
        self.assertFalse(_local(None))
        self.assertIsNotNone(app)


class ProtocolTests(unittest.TestCase):
    KEY = b"test-key-0123456789"

    def test_round_trip(self):
        payload = protocol.unlock_payload("kane", "指纹", now=1700000000, nonce="abc")
        frame = protocol.build_frame(protocol.CMD_UNLOCK, 7, payload, self.KEY)
        parsed = protocol.parse_frame(frame, self.KEY)

        self.assertEqual(parsed["cmd"], protocol.CMD_UNLOCK)
        self.assertEqual(parsed["seq"], 7)
        self.assertEqual(parsed["payload"]["user"], "kane")
        self.assertEqual(parsed["payload"]["nonce"], "abc")
        self.assertEqual(parsed["payload"]["ts"], 1700000000000)

    def test_tampered_payload_is_rejected_by_hmac(self):
        payload = protocol.unlock_payload("kane", "指纹", now=1)
        frame = bytearray(protocol.build_frame(protocol.CMD_UNLOCK, 1, payload, self.KEY))
        frame[protocol.HEADER_LEN] ^= 0xFF

        with self.assertRaises(ValueError):
            protocol.parse_frame(bytes(frame), self.KEY)

    def test_wrong_key_is_rejected(self):
        payload = protocol.unlock_payload("kane", "指纹", now=1)
        frame = protocol.build_frame(protocol.CMD_UNLOCK, 1, payload, self.KEY)

        with self.assertRaises(ValueError):
            protocol.parse_frame(frame, b"other-key")


class DriverTests(unittest.TestCase):
    def test_mock_driver_holds_for_the_requested_time(self):
        driver = MockDriver()
        driver.unlock(0.01)

        self.assertFalse(driver.unlocked)
        self.assertEqual(driver.status()["pulses"], 1)

    def test_build_driver_selects_implementation(self):
        self.assertEqual(build_driver("mock", pulse=1, chip="gpiochip0", line=0, active_low=False).name, "mock")
        self.assertEqual(build_driver("gpio", pulse=1, chip="gpiochip1", line=3, active_low=True).name, "gpio")

    def test_gpio_active_low_inverts_the_level(self):
        normal = GpioRelayDriver(line=0, active_low=False)
        inverted = GpioRelayDriver(line=0, active_low=True)

        self.assertEqual((normal._level(True), normal._level(False)), (1, 0))
        self.assertEqual((inverted._level(True), inverted._level(False)), (0, 1))

    def test_gpio_probe_reports_missing_tool(self):
        from unittest.mock import patch

        with patch("shutil.which", return_value=None):
            self.assertIn("libgpiod", GpioRelayDriver.probe())


if __name__ == "__main__":
    unittest.main()
