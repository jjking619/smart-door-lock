import os
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui.api import parse_event, parse_recording
import ui.page_records as page_records_module
from ui.page_records import RecordsPage
from ui.app import MainWindow


class FakeApi:
    base_url = "http://localhost:8083"
    token = ""

    def events(self, limit, on_ok, on_err=None):
        on_ok([])

    def recordings(self, limit, kind, on_ok, on_err=None):
        on_ok([
            parse_recording({
                "path": "clips/20261106/211330_613.mp4",
                "kind": "event",
                "ts": "2026-11-06T21:13:30.613+08:00",
                "size": 289427,
                "event_type": "intrusion",
                "severity": "warn",
                "snapshot": None,
            }),
            parse_recording({
                "path": "recordings/continuous/20261106/202702_925.mp4",
                "kind": "continuous",
                "ts": "2026-11-06T20:27:02.925+08:00",
                "size": 4368029,
                "event_type": None,
                "severity": None,
                "snapshot": None,
            }),
            parse_recording({
                "path": "recordings/continuous/20261105/230001_000.mp4",
                "kind": "continuous",
                "ts": "2026-11-05T23:00:01.000+08:00",
                "size": 1024,
                "event_type": None,
                "severity": None,
                "snapshot": None,
            }),
        ])

    def media_url(self, path):
        return f"http://localhost:8083/media/{path}"

    def get_bytes(self, path, on_ok, on_err=None):
        if on_err:
            on_err("no thumb")

    def get_bytes_url(self, url, on_ok, on_err=None):
        if on_err:
            on_err("skip")


class DeferredApi(FakeApi):
    def __init__(self):
        self.event_requests = []
        self.recording_requests = []
        self.image_requests = []

    def events(self, limit, on_ok, on_err=None):
        self.event_requests.append((on_ok, on_err))

    def recordings(self, limit, kind, on_ok, on_err=None):
        self.recording_requests.append((on_ok, on_err))

    def get_bytes(self, path, on_ok, on_err=None):
        self.image_requests.append((path, on_ok, on_err))


def events(count):
    return [parse_event({"type": "package.detected", "ts": 1700000000 + i}) for i in range(count)]


def recordings(count):
    return [parse_recording({
        "path": f"clips/20261106/{i}.mp4",
        "kind": "event",
        "snapshot": f"snapshots/{i}.jpg",
    }) for i in range(count)]


class RecordsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.multimedia = patch.object(page_records_module, "HAVE_MULTIMEDIA", False)
        self.multimedia.start()
        self.addCleanup(self.multimedia.stop)

    def wait_for(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(condition())

    def test_recording_filters_and_group_toggle(self):
        page = RecordsPage(FakeApi())
        page.refresh()
        page._set_tab("recordings")

        self.assertEqual(page.sub.text(), "录像 3 条 · 全部")

        page._set_recording_kind("continuous")
        self.assertEqual(page.sub.text(), "录像 2 条 · 在线录像")

        first = page.recordings_list.item(0)
        self.assertEqual(first.data(Qt.ItemDataRole.UserRole)["type"], "group")
        before = page.recordings_list.count()
        page._on_recordings_item_clicked(first)
        after = page.recordings_list.count()
        self.assertLess(after, before)

        page.stop()
        page.close()

    def test_lazy_requests_and_fast_tab_switch(self):
        api = DeferredApi()
        page = RecordsPage(api)
        page.refresh()
        page.refresh()
        self.assertEqual(len(api.event_requests), 1)
        self.assertEqual(len(api.recording_requests), 0)
        self.assertEqual(page.sub.text(), "正在载入…")

        page._set_tab("recordings")
        self.assertEqual(len(api.recording_requests), 1)
        self.assertEqual(page.sub.text(), "正在载入…")
        api.event_requests[0][0](events(120))
        self.assertEqual(page.events_list.count(), 0)
        self.assertEqual(page.sub.text(), "正在载入…")
        api.recording_requests[0][0](recordings(300))
        self.assertEqual(page.recordings_list.count(), 301)
        last = page.recordings_list.item(300).data(Qt.ItemDataRole.UserRole)
        self.assertEqual(last["record"].path, "clips/20261106/299.mp4")
        self.assertEqual(len(api.image_requests), 0)

        page._set_tab("events")
        self.assertEqual(page.events_list.count(), 120)
        self.assertIsNone(page.events_list.itemWidget(page.events_list.item(0)))
        self.assertEqual(len(api.event_requests), 1)
        page.close()

    def test_errors_and_source_change_ignore_stale_replies(self):
        api = DeferredApi()
        page = RecordsPage(api)
        page.refresh()
        old_ok = api.event_requests[0][0]
        page.invalidate()
        page.refresh()
        old_ok(events(2))
        self.assertEqual(page.sub.text(), "正在载入…")
        api.event_requests[1][1]("连接失败")
        self.assertEqual(page.sub.text(), "事件读取失败：连接失败")
        self.assertTrue(page.empty.isHidden())
        page.refresh()
        api.event_requests[2][0](events(1))
        self.assertEqual(page.sub.text(), "事件 1 条")
        self.assertEqual(page.events_list.count(), 1)
        page.close()

    def test_visible_thumbnails_are_bounded_cached_and_safe_after_rebuild(self):
        api = DeferredApi()
        page = RecordsPage(api)
        page.resize(1000, 600)
        page.show()
        page._set_tab("recordings")
        api.recording_requests[0][0](recordings(30))
        self.wait_for(lambda: bool(api.image_requests))
        self.assertGreater(len(api.image_requests), 0)
        self.assertLessEqual(len(api.image_requests), 6)

        image = QPixmap(72, 40)
        image.fill(Qt.GlobalColor.red)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "JPG")
        path, callback, _ = api.image_requests[0]
        page._set_recording_kind("continuous")
        callback(bytes(data))
        self.assertIn(path, page._thumb_cache)
        page._set_recording_kind("all")
        QTest.qWait(50)
        self.assertEqual(sum(req[0] == path for req in api.image_requests), 1)
        page.invalidate()
        if len(api.image_requests) > 1:
            api.image_requests[1][1](bytes(data))
        self.assertFalse(page._thumb_cache)
        page.close()

    def test_navigation_does_not_preload_or_duplicate_inflight_request(self):
        api = DeferredApi()
        with patch("ui.app.ApiClient.events", side_effect=api.events), \
             patch("ui.app.ApiClient.recordings", side_effect=api.recordings), \
             patch("ui.page_home.HomePage.start"), \
             patch("ui.page_home.HomePage.refresh"):
            window = MainWindow("http://127.0.0.1:1")
            self.assertEqual(len(api.event_requests), 0)
            window.goto("records")
            window.goto("records")
            window.goto("home")
            window.goto("records")
            self.assertEqual(len(api.event_requests), 1)
            api.event_requests[0][0](events(1))
            window.goto("home")
            window.goto("records")
            self.assertEqual(len(api.event_requests), 2)
            self.assertEqual(len(api.recording_requests), 0)
            window.close()

    def test_record_selection_preserves_playback_url(self):
        api = DeferredApi()
        page = RecordsPage(api)
        page._set_tab("recordings")
        api.recording_requests[0][0](recordings(2))
        page._on_recordings_item_clicked(page.recordings_list.item(1))
        self.assertTrue(page.btn_open.isEnabled())
        self.assertEqual(page._external_url, "http://localhost:8083/media/clips/20261106/0.mp4")
        page.close()

    def test_event_selection_preserves_playback_url(self):
        api = DeferredApi()
        page = RecordsPage(api)
        page.refresh()
        event = parse_event({
            "type": "package.detected",
            "clip": "clips/20261106/event.mp4",
        })
        api.event_requests[0][0]([event])
        page.events_list.itemClicked.emit(page.events_list.item(0))
        self.assertTrue(page.btn_open.isEnabled())
        self.assertEqual(page._external_url, "http://localhost:8083/media/clips/20261106/event.mp4")
        page.close()

    def test_first_event_is_selected_and_empty_clip_clears_previous_video(self):
        api = DeferredApi()
        page = RecordsPage(api)
        page.refresh()
        first = parse_event({"type": "package.detected", "clip": "clips/first.mp4"})
        second = parse_event({"type": "person.detected"})
        api.event_requests[0][0]([first, second])
        self.assertEqual(page.events_list.currentRow(), 0)
        self.assertEqual(page._external_url, "http://localhost:8083/media/clips/first.mp4")
        page.events_list.itemClicked.emit(page.events_list.item(1))
        self.assertIsNone(page._external_url)
        self.assertFalse(page.btn_open.isEnabled())
        self.assertEqual(page.video_placeholder.text(), "此事件没有录像")
        self.assertIs(page.video_stack.currentWidget(), page.video_placeholder)
        page.close()

    def test_first_recording_is_selected_instead_of_group_header(self):
        api = DeferredApi()
        page = RecordsPage(api)
        page._set_tab("recordings")
        api.recording_requests[0][0](recordings(2))
        self.assertEqual(page.recordings_list.currentRow(), 1)
        self.assertEqual(page._external_url, "http://localhost:8083/media/clips/20261106/0.mp4")
        page.close()

    def test_empty_and_failed_load_show_placeholder(self):
        api = DeferredApi()
        page = RecordsPage(api)
        page.refresh()
        api.event_requests[0][0]([])
        self.assertEqual(page.video_placeholder.text(), "暂无事件")
        self.assertIs(page.video_stack.currentWidget(), page.video_placeholder)
        page.refresh()
        api.event_requests[1][1]("连接失败")
        self.assertEqual(page.video_placeholder.text(), "记录读取失败，请刷新重试")
        self.assertIn("连接失败", page.sub.text())
        page.close()

    def test_manual_selection_wins_over_pending_refresh(self):
        api = DeferredApi()
        page = RecordsPage(api)
        page.refresh()
        first = parse_event({"type": "package.detected", "clip": "clips/first.mp4"})
        selected = parse_event({"type": "person.detected", "clip": "clips/selected.mp4"})
        api.event_requests[0][0]([first, selected])
        page.pause_loading()
        page.resume_loading()
        page.refresh()
        page.play_event(selected)
        api.event_requests[1][0]([first, selected])
        self.assertEqual(page._external_url, "http://localhost:8083/media/clips/selected.mp4")
        page.close()

    def test_unfinished_clip_retries_and_selection_change_cancels(self):
        api = DeferredApi()
        page = RecordsPage(api)
        player = Mock()
        player.error.return_value = QMediaPlayer.Error.FormatError
        player.errorString.return_value = "Could not open file"
        page.player = player
        with patch.object(page_records_module, "HAVE_MULTIMEDIA", True):
            page._on_event_selected(parse_event({"type": "package.detected", "clip": "clips/first.mp4"}))
            page._on_player_error()
            self.assertTrue(page._retry_timer.isActive())
            self.assertIn("正在重试", page.video_placeholder.text())
            self.assertEqual(page.btn_play.text(), "▶  播放")
            page._toggle()
            self.assertFalse(page._retry_timer.isActive())
            self.assertGreaterEqual(player.play.call_count, 2)
            page._on_event_selected(parse_event({"type": "person.detected"}))
            self.assertFalse(page._retry_timer.isActive())
            self.assertIsNone(page._external_url)
        page.close()

    def test_pause_defers_hidden_response_rendering(self):
        api = DeferredApi()
        page = RecordsPage(api)
        page.refresh()
        page.pause_loading()
        api.event_requests[0][0](events(20))
        self.assertEqual(page.events_list.count(), 0)
        page.resume_loading()
        self.assertEqual(page.events_list.count(), 20)
        page.close()


if __name__ == "__main__":
    unittest.main()