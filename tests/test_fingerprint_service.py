import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from fingerprint.service import (
    FingerprintService,
    FingerprintOperationError,
    NOFINGER,
    NOTFOUND,
    OK,
    create_app,
    find_serial_device,
)


class FakeSensor:
    def __init__(self):
        self.library_size = 300
        self.templates = []
        self.template_count = 0
        self.finger_id = 0
        self.confidence = 500
        self.images = [OK, NOFINGER, OK, NOFINGER]
        self.tz_codes = []
        self.model_codes = []

    def check_module(self):
        return True

    def read_templates(self):
        return OK

    def count_templates(self):
        self.template_count = len(self.templates)
        return OK

    def get_image(self):
        return self.images.pop(0) if self.images else NOFINGER

    def image_2_tz(self, _slot):
        return self.tz_codes.pop(0) if self.tz_codes else OK

    def create_model(self):
        return self.model_codes.pop(0) if self.model_codes else OK

    def store_model(self, location, _slot):
        self.templates.append(location)
        return OK

    def delete_model(self, location):
        self.templates.remove(location)
        return OK


class FingerprintServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = FingerprintService(data_dir=self.temp.name, operation_timeout=5)
        self.sensor = FakeSensor()
        self.service._sensor = self.sensor
        self.service._template_locations = []
        self.service._set_state(connected=True, device="/dev/ttyUSB0", library_size=300)
        # 默认不真的去连 lockd，避免单测里发网络请求
        self.service._unlock_notifier = lambda *_args: None

    def test_enrollment_records_metadata_and_does_not_save_biometric_data(self):
        self.service._enroll("张三", "右手食指")

        self.assertEqual(self.sensor.templates, [0])
        self.assertEqual(self.service.templates(), [{
            "id": 0, "name": "张三", "finger": "右手食指", "registered": True,
        }])
        saved = Path(self.temp.name, "fingerprint", "users.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(saved)[0]["id"], 0)
        self.assertNotIn("template", saved.lower())
        events = Path(self.temp.name, "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(events[-1])["type"], "finger.enrolled")

    def test_second_enrollment_uses_next_free_location(self):
        self.service._enroll("张三", "右手食指")
        self.sensor.images = [OK, NOFINGER, OK, NOFINGER]
        self.service._enroll("李四", "左手中指")

        self.assertEqual(self.sensor.templates, [0, 1])
        self.assertEqual([item["id"] for item in self.service.templates()], [0, 1])

    def test_delete_rejects_missing_template_and_removes_managed_template(self):
        with self.assertRaisesRegex(ValueError, "未找到模块"):
            self.service.begin_delete(7)

        self.service._users[0] = {"name": "张三", "finger": "食指", "created_at": ""}
        self.sensor.templates = [0]
        self.service._template_locations = [0]
        self.service._delete(0)
        self.assertEqual(self.sensor.templates, [])
        self.assertEqual(self.service._users, {})
        self.assertEqual(self.service._template_locations, [])
        self.assertIn("指纹已删除", self.service.status()["notice"])

    def test_delete_unregistered_template_removes_only_selected_location(self):
        self.sensor.templates = [7, 9]
        self.service._template_locations = [7, 9]
        self.service.begin_delete(7)
        self.assertEqual(self.service._commands.get_nowait(), ("delete", {"id": 7}))
        self.service._delete(7)
        self.assertEqual(self.sensor.templates, [9])
        self.assertEqual(self.service._template_locations, [9])
        self.assertEqual(self.service._users, {})
        self.assertIn("未登记模板", self.service.status()["notice"])

    def test_refresh_reads_hardware_and_updates_cached_list(self):
        self.sensor.templates = [7, 9]
        self.sensor.read_templates = Mock(return_value=OK)
        self.sensor.count_templates = Mock(return_value=OK)
        self.sensor.template_count = 2
        self.service.begin_refresh()
        self.assertEqual(self.service._commands.get_nowait(), ("refresh", {}))
        self.service._refresh_templates()
        self.sensor.read_templates.assert_called_once_with()
        self.sensor.count_templates.assert_called_once_with()
        self.assertEqual([item["id"] for item in self.service.templates()], [7, 9])
        self.assertEqual(self.service.status()["template_count"], 2)
        self.assertIsNone(self.service.status()["action"])
        self.assertEqual(self.service.status()["notice"], "刷新完成")

    def test_refresh_failure_does_not_replace_cached_list(self):
        self.service._template_locations = [7]
        self.sensor.read_templates = Mock(return_value=0x18)
        with self.assertRaisesRegex(FingerprintOperationError, "FLASH"):
            self.service._refresh_templates()
        self.assertEqual(self.service._template_locations, [7])

    def test_refresh_is_rejected_during_enrollment(self):
        self.service._set_state(action="enroll")
        with self.assertRaisesRegex(RuntimeError, "正在进行"):
            self.service.begin_refresh()

    def test_delete_failure_to_save_metadata_keeps_sensor_and_ui_state_consistent(self):
        self.service._users[0] = {"name": "张三", "finger": "食指", "created_at": ""}
        self.sensor.templates = [0]
        self.service._template_locations = [0]
        with patch.object(self.service, "_save_users", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "模板已删除"):
                self.service._delete(0)

        self.assertEqual(self.sensor.templates, [])
        self.assertEqual(self.service._users, {})
        self.assertEqual(self.service._template_locations, [])
        self.assertEqual(self.service.status()["template_count"], 0)

    def test_rejects_enrollment_when_device_is_offline(self):
        self.service._set_state(connected=False)
        with self.assertRaisesRegex(RuntimeError, "未连接"):
            self.service.begin_enroll("张三", "食指")

    def test_normal_search_does_not_load_or_compare_templates(self):
        self.sensor.finger_fast_search = Mock(return_value=OK)
        self.sensor.load_model = Mock()
        self.assertEqual(self.service._search(), OK)
        self.sensor.load_model.assert_not_called()

    def test_undocumented_search_code_compares_in_buffer_two_until_match(self):
        self.sensor.finger_fast_search = Mock(return_value=0x17)
        self.service._template_locations = [0, 88]
        self.sensor.load_model = Mock(return_value=OK)
        self.sensor.compare_templates = Mock(side_effect=[0x08, OK])
        self.sensor.confidence = (97,)
        self.assertEqual(self.service._search(), OK)
        self.assertEqual(self.sensor.finger_id, 88)
        self.assertEqual(self.sensor.confidence, 97)
        self.assertEqual(
            self.sensor.load_model.call_args_list,
            [((0, 2),), ((88, 2),)],
        )

    def test_undocumented_search_code_is_not_itself_a_match(self):
        self.sensor.finger_fast_search = Mock(return_value=0x17)
        self.service._template_locations = [0]
        self.sensor.load_model = Mock(return_value=OK)
        self.sensor.compare_templates = Mock(return_value=0x08)
        self.assertEqual(self.service._search(), NOTFOUND)

    def test_fallback_read_and_compare_errors_are_not_hidden(self):
        self.sensor.finger_fast_search = Mock(return_value=0x17)
        self.service._template_locations = [0]
        self.sensor.load_model = Mock(return_value=0x0C)
        with self.assertRaisesRegex(FingerprintOperationError, "读取模板 0"):
            self.service._search()
        self.sensor.load_model.return_value = OK
        self.sensor.compare_templates = Mock(return_value=0x18)
        with self.assertRaisesRegex(FingerprintOperationError, "FLASH"):
            self.service._search()

    def test_scan_records_fallback_match_and_waits_for_release(self):
        self.sensor.finger_fast_search = Mock(return_value=0x17)
        self.service._template_locations = [0]
        self.sensor.load_model = Mock(return_value=OK)
        self.sensor.compare_templates = Mock(return_value=OK)
        self.sensor.confidence = (142,)
        self.service._users[0] = {"name": "张三", "finger": "食指"}
        self.sensor.images = [OK, NOFINGER]
        self.service._scan_once()
        self.assertEqual(self.service.status()["last_match"]["id"], 0)
        self.assertEqual(self.service.status()["last_match"]["confidence"], 142)
        event = json.loads(Path(self.temp.name, "events.jsonl").read_text().splitlines()[-1])
        self.assertEqual(event["type"], "finger.match")
        self.assertEqual(self.sensor.images, [])

    def _match_once(self) -> None:
        self.sensor.finger_fast_search = Mock(return_value=OK)
        self.sensor.finger_id = 0
        self.sensor.confidence = 95
        self.service._users[0] = {"name": "张三", "finger": "食指"}
        self.sensor.images = [OK, NOFINGER]
        self.service._scan_once()

    def test_match_hands_the_decision_to_lockd(self):
        asked: list[tuple] = []
        self.service._unlock_notifier = lambda *args: asked.append(args) or {"ok": True}
        self._match_once()

        self.assertEqual(asked, [(0, "张三", 95)])
        self.assertTrue(self.service.status()["last_unlock"]["ok"])

    def test_refusal_is_kept_for_the_ui_without_blocking_scanning(self):
        self.service._unlock_notifier = lambda *_a: {"ok": False, "reason": "该指纹未在界面登记，不能开门"}
        self._match_once()

        unlock = self.service.status()["last_unlock"]
        self.assertFalse(unlock["ok"])
        self.assertIn("未在界面登记", unlock["reason"])

    def test_notifier_failure_never_breaks_the_scan_loop(self):
        def boom(*_args):
            raise OSError("lockd 挂了")

        self.service._unlock_notifier = boom
        self._match_once()

        self.assertEqual(self.service.status()["last_match"]["id"], 0)
        self.assertFalse(self.service.status()["last_unlock"]["ok"])

    def test_default_notifier_is_disabled_by_env(self):
        from fingerprint.service import _default_unlock_notifier

        with patch.dict("os.environ", {"FINGERPRINT_UNLOCK": "0"}, clear=False):
            self.assertIsNone(_default_unlock_notifier(0, "张三", 90))

    def test_scan_waits_for_release_on_search_errors(self):
        self.sensor.images = [OK, NOFINGER]
        self.sensor.finger_fast_search = Mock(return_value=0x18)
        with self.assertRaisesRegex(FingerprintOperationError, "FLASH"):
            self.service._scan_once()
        self.assertEqual(self.sensor.images, [])
        self.sensor.images = [OK, NOFINGER]
        self.sensor.finger_fast_search.return_value = 0x17
        self.service._template_locations = [0]
        self.sensor.load_model = Mock(return_value=0x0C)
        with self.assertRaisesRegex(FingerprintOperationError, "读取模板"):
            self.service._scan_once()
        self.assertEqual(self.sensor.images, [])

    def test_transient_get_image_errors_are_retried_like_manual_flowchart(self):
        self.sensor.images = [0x03, OK, 0x03, NOFINGER, 0x01, OK, NOFINGER]
        self.service._enroll("张三", "右手食指")

        self.assertEqual(self.sensor.templates, [0])
        self.assertIn("录入成功", self.service.status()["notice"])

    def test_poor_quality_press_asks_for_another_press_instead_of_aborting(self):
        self.sensor.images = [OK, NOFINGER, OK, NOFINGER, OK, NOFINGER]
        self.sensor.tz_codes = [0x07]
        self.service._enroll("张三", "右手食指")

        self.assertEqual(self.sensor.templates, [0])

    def test_merge_failure_restarts_both_presses(self):
        self.sensor.images = [OK, NOFINGER, OK, NOFINGER, OK, NOFINGER, OK]
        self.sensor.model_codes = [0x0A]
        self.service._enroll("张三", "右手食指")

        self.assertEqual(self.sensor.templates, [0])

    def test_unrecoverable_feature_error_still_ends_enrollment(self):
        self.sensor.tz_codes = [0x18]
        with self.assertRaisesRegex(RuntimeError, "FLASH"):
            self.service._enroll("张三", "右手食指")
        self.assertIsNone(self.service.status()["action"])

    def test_bad_reply_frame_flushes_input_and_resends_once(self):
        class FakeUart:
            flushed = 0

            def reset_input_buffer(self):
                FakeUart.flushed += 1

        replies = [RuntimeError("Incorrect packet data"), NOFINGER]

        def flaky_get_image():
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        self.service._serial = FakeUart()
        self.sensor.get_image = flaky_get_image
        self.assertEqual(self.service._call("get_image"), NOFINGER)
        self.assertEqual(FakeUart.flushed, 1)

    def test_connect_tolerates_modules_without_getecho(self):
        class Uart:
            closed = False

            def reset_input_buffer(self):
                pass

            def close(self):
                self.closed = True

        sensor = FakeSensor()
        sensor.check_module = lambda: (_ for _ in ()).throw(RuntimeError("Something is wrong"))
        service = FingerprintService(
            data_dir=self.temp.name,
            serial_factory=lambda *_args, **_kwargs: Uart(),
            sensor_factory=lambda _uart: sensor,
            serial_resolver=lambda: "/dev/ttyUSB0",
        )
        with patch("fingerprint.service.POWER_ON_DELAY", 0):
            service._connect()
        self.assertTrue(service.status()["connected"])

    def test_real_serial_port_is_opened_exclusively(self):
        import serial

        opened = {}

        def fake_serial(port, **kwargs):
            opened.update(kwargs, port=port)
            raise serial.SerialException("busy")

        service = FingerprintService(data_dir=self.temp.name, serial_resolver=lambda: "/dev/ttyUSB0")
        with patch("serial.Serial", side_effect=fake_serial):
            with self.assertRaises(serial.SerialException):
                service._connect()
        self.assertTrue(opened["exclusive"])
        self.assertEqual(opened["baudrate"], 57600)

    def test_auto_discovers_primary_board_uart(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch("fingerprint.service.glob.glob", return_value=[]):
                with patch("fingerprint.service.os.path.exists",
                           side_effect=lambda path: path == "/dev/serial0"):
                    self.assertEqual(find_serial_device(), "/dev/serial0")

    def test_configured_uart_takes_precedence_over_discovery(self):
        with patch.dict("os.environ", {"FINGERPRINT_SERIAL": "/dev/serial1"}, clear=True):
            with patch("fingerprint.service.glob.glob") as glob:
                self.assertEqual(find_serial_device(), "/dev/serial1")
                glob.assert_not_called()


class FingerprintHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = FingerprintService(data_dir=self.temp.name)
        self.client = TestClient(TestServer(create_app(self.service)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_management_operations_do_not_require_pin(self):
        with patch.dict("os.environ", {"FINGERPRINT_ADMIN_PIN": "7391"}), \
                patch.object(self.service, "begin_enroll") as begin_enroll:
            response = await self.client.post("/enroll", json={"name": "张三", "finger": "食指"})
        self.assertEqual(response.status, 202)
        begin_enroll.assert_called_once_with("张三", "食指")

    async def test_health_does_not_report_admin_lock(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)
        self.assertNotIn("admin_configured", await response.json())

    async def test_refresh_queues_a_hardware_operation(self):
        self.service._set_state(connected=True)
        response = await self.client.post("/refresh", json={})
        self.assertEqual(response.status, 202)
        self.assertEqual(self.service._commands.get_nowait(), ("refresh", {}))
        response = await self.client.post("/refresh", json={})
        self.assertEqual(response.status, 409)

    async def test_refresh_offline_returns_error(self):
        response = await self.client.post("/refresh", json={})
        self.assertEqual(response.status, 409)
        self.assertIn("未连接", (await response.json())["error"])

    async def test_delete_unregistered_template_is_queued(self):
        self.service._set_state(connected=True)
        self.service._template_locations = [7]
        response = await self.client.post("/delete", json={"id": 7})
        self.assertEqual(response.status, 202)
        self.assertEqual(self.service._commands.get_nowait(), ("delete", {"id": 7}))


if __name__ == "__main__":
    unittest.main()
