import os
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

from ui import theme
from ui.api import parse_event, parse_recording
import ui.page_records as page_records_module
from ui.page_events import DetailPanel
from ui.page_records import RecordsPage
from ui.stream import VideoView
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

    def snapshot_url(self):
        return "http://localhost:8083/snapshot.jpg"


class DeferredApi(FakeApi):
    def __init__(self):
        self.event_requests = []
        self.recording_requests = []
        self.image_requests = []
        self.image_url_requests = []

    def events(self, limit, on_ok, on_err=None):
        self.event_requests.append((on_ok, on_err))

    def recordings(self, limit, kind, on_ok, on_err=None):
        self.recording_requests.append((on_ok, on_err))

    def get_bytes(self, path, on_ok, on_err=None):
        self.image_requests.append((path, on_ok, on_err))

    def get_bytes_url(self, url, on_ok, on_err=None):
        self.image_url_requests.append((url, on_ok, on_err))


def events(count):
    return [parse_event({"type": "package.detected", "ts": 1700000000 + i}) for i in range(count)]


def recordings(count):
    return [parse_recording({
        "path": f"clips/20261106/{i}.mp4",
        "kind": "event",
        "snapshot": f"snapshots/{i}.jpg",
    }) for i in range(count)]


def image_bytes():
    image = QPixmap(640, 360)
    image.fill(Qt.GlobalColor.red)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPG")
    return bytes(data)


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
        self.assertEqual(page.events_list.count(), 121)
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
        self.assertEqual(page.sub.text(), "最近载入 1 条 · 全部 1 条")
        self.assertEqual(page.events_list.count(), 2)
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

    def test_continuous_recordings_hide_thumbnails_but_events_keep_them(self):
        api = DeferredApi()
        page = RecordsPage(api)
        self.addCleanup(page.close)
        page.resize(1000, 600)
        page.show()
        page._set_tab("recordings")
        continuous = parse_recording({
            "path": "recordings/continuous/example.mp4",
            "kind": "continuous",
            "snapshot": "snapshots/continuous.jpg",
        })
        event = parse_recording({
            "path": "clips/example.mp4",
            "kind": "event",
            "snapshot": "snapshots/event.jpg",
        })
        api.recording_requests[0][0]([continuous, event])
        self.wait_for(lambda: bool(api.image_requests))
        self.assertEqual([request[0] for request in api.image_requests], ["snapshots/event.jpg"])

        delegate = page.recordings_list.itemDelegate()
        for row, expected_x, has_placeholder in ((1, 14, False), (2, 94, True)):
            with self.subTest(row=row):
                index = page.recordings_list.model().index(row, 0)
                option = QStyleOptionViewItem()
                option.rect = page.recordings_list.visualRect(index)
                option.widget = page.recordings_list
                image = QImage(option.rect.size(), QImage.Format.Format_ARGB32)
                image.fill(Qt.GlobalColor.transparent)
                painter = QPainter(image)
                with patch.object(page_records_module, "_draw_text") as draw:
                    delegate.paint(painter, option, index)
                painter.end()
                texts = [call.args[2] for call in draw.call_args_list]
                self.assertEqual("无图" in texts, has_placeholder)
                title = next(call for call in draw.call_args_list
                             if call.args[2] == ("在线录像" if row == 1 else "事件片段"))
                self.assertEqual(title.args[1].left() - option.rect.left(), expected_x)

        page._on_recordings_item_clicked(page.recordings_list.item(1))
        self.assertEqual(page._current_record.path, continuous.path)

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
        page.events_list.itemClicked.emit(page.events_list.item(1))
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
        self.assertEqual(page.events_list.currentRow(), 1)
        self.assertEqual(page._external_url, "http://localhost:8083/media/clips/first.mp4")
        page.events_list.itemClicked.emit(page.events_list.item(2))
        self.assertIsNone(page._external_url)
        self.assertFalse(page.btn_open.isEnabled())
        self.assertEqual(page.video_placeholder.text(), "此事件没有录像")
        self.assertIs(page.video_stack.currentWidget(), page.video_placeholder)
        self.assertTrue(page.video_stack.isHidden())
        self.assertFalse(page.event_details.isHidden())
        self.assertTrue(page.btn_play.isHidden())
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
            source_calls = player.setSource.call_count
            play_calls = player.play.call_count
            page.btn_event_details.click()
            page.btn_event_details.click()
            self.assertEqual(player.setSource.call_count, source_calls)
            self.assertEqual(player.play.call_count, play_calls)
            page._on_player_error()
            self.assertTrue(page._retry_timer.isActive())
            self.assertIn("正在重试", page.video_placeholder.text())
            self.assertIn("Could not open file", page.video_placeholder.text())
            self.assertEqual(page.btn_play.text(), "▶  播放")
            page._toggle()
            self.assertFalse(page._retry_timer.isActive())
            self.assertGreaterEqual(player.play.call_count, 2)
            page._on_event_selected(parse_event({"type": "person.detected"}))
            self.assertFalse(page._retry_timer.isActive())
            self.assertIsNone(page._external_url)
        page.close()

    def test_playback_and_external_download_errors_remain_readable(self):
        page = RecordsPage(FakeApi())
        self.addCleanup(page.close)
        player = Mock()
        player.error.return_value = QMediaPlayer.Error.NetworkError
        player.errorString.return_value = "network connection timed out"
        page.player = player
        with patch.object(page_records_module, "HAVE_MULTIMEDIA", True):
            page._on_event_selected(parse_event({"type": "intrusion", "clip": "clips/first.mp4"}))
            page._on_player_error()
            self.assertIn("network connection timed out", page.video_placeholder.text())
            self.assertIn("可用系统播放器打开", page.video_placeholder.text())
            page._open_external()
            self.assertFalse(page.media_error.isHidden())
            self.assertEqual(page.media_error.text(), "下载失败：skip")
            page._on_event_selected(parse_event({"type": "finger.match"}))
            self.assertTrue(page.media_error.isHidden())

    def test_pause_defers_hidden_response_rendering(self):
        api = DeferredApi()
        page = RecordsPage(api)
        page.refresh()
        page.pause_loading()
        api.event_requests[0][0](events(20))
        self.assertEqual(page.events_list.count(), 0)
        page.resume_loading()
        self.assertEqual(page.events_list.count(), 21)
        page.close()

    def test_event_classification(self):
        expected = {
            "person.detected": "intrusion", "intrusion": "intrusion", "tamper": "intrusion",
            "finger.match": "access", "finger.reject": "access", "unlock.remote": "access",
            "unlock.request": "access", "door.opened": "access", "door.closed": "access",
            "package.detected": "package", "package.picked": "package",
            "finger.enrolled": "system", "finger.deleted": "system",
            "finger.renamed": "system", "new.event": "system",
        }
        for kind, category in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(parse_event({"type": kind}).category, category)

    def test_event_filters_counts_and_empty_selection(self):
        api = DeferredApi()
        page = RecordsPage(api)
        self.addCleanup(page.close)
        page.refresh()
        api.event_requests[0][0]([
            parse_event({"type": kind})
            for kind in ("tamper", "person.detected", "finger.reject", "finger.enrolled")
        ])
        self.assertEqual(page._event_chips["all"].text(), "全部 4")
        self.assertEqual(page._event_chips["intrusion"].text(), "安全 2")
        self.assertEqual(page._event_chips["access"].text(), "出入 1")
        self.assertEqual(page._event_chips["system"].text(), "系统 1")
        self.assertEqual(page._event_chips["package"].text(), "快递 0")

        page._event_chips["access"].click()
        self.assertEqual(page.sub.text(), "最近载入 4 条 · 出入 1 条")
        self.assertEqual(page.events_list.count(), 2)
        self.assertEqual(page._current_event.kind, "finger.reject")
        self.assertEqual(page.event_title.text(), "指纹验证失败")
        self.assertEqual(sum(button.isChecked() for button in page._event_chips.values()), 1)
        page._event_chips["package"].click()
        self.assertEqual(page.events_list.count(), 0)
        self.assertEqual(page.empty.text(), "暂无快递事件")
        self.assertFalse(page.empty.isHidden())
        self.assertTrue(page.event_details.isHidden())
        self.assertIsNone(page._external_url)
        self.assertIsNone(page._current_event)
        self.assertEqual(page.video_placeholder.text(), "暂无快递事件")
        self.assertEqual(len(api.event_requests), 1)

    def test_event_dates_are_sorted_grouped_and_collapsible(self):
        api = DeferredApi()
        page = RecordsPage(api)
        self.addCleanup(page.close)
        now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        yesterday = now - timedelta(days=1)
        oldest = now - timedelta(days=3)
        items = [
            parse_event({"type": "finger.match", "ts": yesterday.isoformat()}),
            parse_event({"type": "package.detected"}),
            parse_event({"type": "intrusion", "ts": oldest.isoformat()}),
            parse_event({"type": "person.detected", "ts": now.isoformat()}),
            parse_event({"type": "tamper", "ts": (now + timedelta(seconds=1)).isoformat()}),
        ]
        page.refresh()
        api.event_requests[0][0](items)
        headers = [
            page.events_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(page.events_list.count())
            if isinstance(page.events_list.item(i).data(Qt.ItemDataRole.UserRole), dict)
        ]
        self.assertEqual([header["day"] for header in headers], [
            now.date().isoformat(), yesterday.date().isoformat(),
            oldest.date().isoformat(), "未知日期",
        ])
        self.assertEqual([header["count"] for header in headers], [2, 1, 1, 1])
        self.assertEqual(page._current_event.kind, "tamper")
        self.assertEqual(page.events_list.currentRow(), 1)
        self.assertEqual(page_records_module._event_day_label(headers[0]["day"]), "今天")
        self.assertEqual(page_records_module._event_day_label(headers[1]["day"]), "昨天")
        before = page.events_list.count()
        page.events_list.itemClicked.emit(page.events_list.item(0))
        self.assertEqual(page.events_list.count(), before - 2)
        self.assertEqual(page._current_event.kind, "tamper")
        self.assertFalse(page._collapsed_days)
        page._set_event_category("intrusion")
        self.assertIn(headers[0]["day"], page._collapsed_event_days)
        page.events_list.itemClicked.emit(page.events_list.item(0))
        self.assertEqual(page.events_list.count(), 5)

    def test_date_group_uses_local_day_for_offset_timestamps(self):
        local = datetime.now().astimezone().replace(hour=0, minute=30, second=0, microsecond=0)
        event = parse_event({"type": "intrusion", "ts": local.timestamp()})
        self.assertEqual(page_records_module._event_day(event), local.date().isoformat())
        self.assertEqual(page_records_module._event_day(parse_event({"type": "intrusion"})), "未知日期")

    def test_text_details_do_not_show_a_video_area_or_controls(self):
        page = RecordsPage(FakeApi())
        self.addCleanup(page.close)
        event = parse_event({
            "type": "unlock.request", "source": "audiood",
            "decision": "deny", "decision_text": "屋主拒绝",
            "text": "<b>访客原话</b>", "reason": "屋主拒绝了请求",
        })
        page._on_event_selected(event)
        self.assertFalse(page.event_details.isHidden())
        self.assertTrue(page.video_stack.isHidden())
        self.assertTrue(page.btn_open.isHidden())
        self.assertTrue(page.btn_play.isHidden())
        self.assertTrue(page.btn_event_details.isHidden())
        self.assertIn("屋主拒绝", page.event_summary.text())
        self.assertIn("访客原话：<b>访客原话</b>", page.event_fields.text())
        self.assertIn("来源：audiood", page.event_fields.text())
        self.assertNotIn("区域", page.event_fields.text())
        self.assertEqual(page.event_fields.textFormat(), Qt.TextFormat.PlainText)

    def test_internal_zone_remains_in_event_data_but_not_in_either_details_view(self):
        event = parse_event({
            "type": "package.detected", "source": "visiond", "zone": "ground",
            "object": "包裹", "bbox": [10, 20, 30, 40],
        })
        self.assertEqual(event.zone, "ground")
        self.assertEqual(event.raw["zone"], "ground")
        self.assertNotIn("区域", dict(event.detail_rows()))
        self.assertEqual(dict(event.detail_rows())["位置"], "10 20 30 40")

        page = RecordsPage(FakeApi())
        panel = DetailPanel()
        self.addCleanup(page.close)
        self.addCleanup(panel.close)
        page._on_event_selected(event)
        panel.show_event(event)
        self.assertNotIn("区域", page.event_fields.text())
        self.assertNotIn("区域", panel.meta.text())
        self.assertIn("来源", panel.meta.text())

    def test_snapshot_only_details_show_scaled_image_without_playback(self):
        api = DeferredApi()
        page = RecordsPage(api)
        self.addCleanup(page.close)
        page.resize(1000, 650)
        page.show()
        page._on_event_selected(parse_event({
            "type": "package.detected", "snapshot": "snapshots/package.jpg",
        }))
        self.assertEqual(api.image_requests[0][0], "snapshots/package.jpg")
        self.assertEqual(page.video_placeholder.text(), "正在载入快照…")
        api.image_requests[0][1](image_bytes())
        self.app.processEvents()
        self.assertIs(page.video_stack.currentWidget(), page.event_snapshot)
        self.assertFalse(page.video_stack.isHidden())
        self.assertTrue(page.event_details.isHidden())
        self.assertFalse(page.btn_event_details.isHidden())
        self.assertTrue(page.btn_play.isHidden())
        self.assertTrue(page.btn_open.isHidden())
        pixmap = page.event_snapshot.pixmap()
        self.assertFalse(pixmap.isNull())
        self.assertLessEqual(pixmap.width(), page.event_snapshot.width())
        self.assertLessEqual(pixmap.height(), page.event_snapshot.height())
        self.assertAlmostEqual(pixmap.width() / pixmap.height(), 640 / 360, delta=0.02)
        page.btn_event_details.click()
        self.app.processEvents()
        self.assertFalse(page.event_details.isHidden())
        self.assertEqual(page.btn_event_details.text(), "收起详情")
        self.assertIs(page.video_stack.currentWidget(), page.event_snapshot)
        page.btn_event_details.click()
        self.app.processEvents()
        self.assertTrue(page.event_details.isHidden())
        self.assertEqual(page.btn_event_details.text(), "查看详情")
        page.resize(828, 496)
        self.app.processEvents()
        self.assertLessEqual(page.event_snapshot.pixmap().width(), page.event_snapshot.width())
        self.assertLessEqual(page.event_snapshot.pixmap().height(), page.event_snapshot.height())

    def test_fingerprint_match_shows_current_camera_snapshot(self):
        api = DeferredApi()
        page = RecordsPage(api)
        self.addCleanup(page.close)
        page.resize(1000, 650)
        page.show()
        page._on_event_selected(parse_event({"type": "finger.match"}))

        self.assertEqual(api.image_url_requests[0][0], "http://localhost:8083/snapshot.jpg")
        self.assertFalse(page.video_stack.isHidden())
        self.assertTrue(page.btn_event_details.isVisible())
        self.assertIn("未附历史抓拍", page.event_snapshot.toolTip())
        api.image_url_requests[0][1](image_bytes())
        self.app.processEvents()
        self.assertIs(page.video_stack.currentWidget(), page.event_snapshot)
        self.assertIsNotNone(page._snapshot_pixmap)

    def test_fingerprint_match_prefers_matching_unlock_snapshot(self):
        api = DeferredApi()
        page = RecordsPage(api)
        self.addCleanup(page.close)
        match = parse_event({
            "type": "finger.match", "ts": "2026-11-11T12:00:00+08:00", "finger_id": 7,
        })
        page._events = [parse_event({
            "type": "door.opened", "ts": "2026-11-11T12:00:01+08:00",
            "user_id": 7, "source": "finger", "snapshot": "snapshots/unlock.jpg",
        })]
        page._on_event_selected(match)

        self.assertEqual(api.image_requests[0][0], "snapshots/unlock.jpg")
        self.assertEqual(page.event_snapshot.toolTip(), "同次开锁抓拍")
        self.assertFalse(api.image_url_requests)

    def test_snapshot_failures_are_explicit_and_stale_replies_are_ignored(self):
        api = DeferredApi()
        page = RecordsPage(api)
        self.addCleanup(page.close)
        snapshot_event = parse_event({"type": "person.detected", "snapshot": "snapshots/first.jpg"})
        page._on_event_selected(snapshot_event)
        old_ok, old_error = api.image_requests[0][1:]
        page._on_event_selected(parse_event({"type": "finger.match"}))
        old_ok(image_bytes())
        old_error("late error")
        self.assertFalse(page.video_stack.isHidden())
        self.assertEqual(page.event_title.text(), "指纹识别成功")
        self.assertIsNone(page._snapshot_pixmap)
        self.assertEqual(api.image_url_requests[0][0], "http://localhost:8083/snapshot.jpg")
        page._on_event_selected(snapshot_event)
        with self.assertLogs("ui.page_records", level="WARNING"):
            api.image_requests[1][1](b"not an image")
        self.assertEqual(page.video_placeholder.text(), "快照读取失败：图片格式无效")
        page._on_event_selected(snapshot_event)
        with self.assertLogs("ui.page_records", level="WARNING"):
            api.image_requests[2][2]("HTTP 404")
        self.assertEqual(page.video_placeholder.text(), "快照读取失败：HTTP 404")
        self.assertIs(page.video_stack.currentWidget(), page.video_placeholder)
        page.invalidate()
        api.image_requests[2][1](image_bytes())
        self.assertIsNone(page._snapshot_pixmap)
        self.assertTrue(page.event_details.isHidden())

    def test_video_details_take_priority_and_recording_tab_restores_controls(self):
        api = DeferredApi()
        page = RecordsPage(api)
        self.addCleanup(page.close)
        page._on_event_selected(parse_event({
            "type": "intrusion", "clip": "clips/event.mp4", "snapshot": "snapshots/event.jpg",
            "dwell_s": 32,
        }))
        self.assertEqual(api.image_requests, [])
        self.assertTrue(page.event_details.isHidden())
        self.assertFalse(page.btn_event_details.isHidden())
        page.btn_event_details.click()
        self.assertFalse(page.event_details.isHidden())
        self.assertIn("停留：32 s", page.event_fields.text())
        self.assertFalse(page.video_stack.isHidden())
        self.assertFalse(page.btn_play.isHidden())
        self.assertTrue(page.btn_open.isEnabled())
        self.assertEqual(page._external_url, "http://localhost:8083/media/clips/event.mp4")
        page.btn_event_details.click()
        self.assertTrue(page.event_details.isHidden())
        self.assertEqual(page._external_url, "http://localhost:8083/media/clips/event.mp4")
        page.btn_event_details.click()
        page._on_event_selected(parse_event({"type": "person.detected", "clip": "clips/next.mp4"}))
        self.assertTrue(page.event_details.isHidden())
        self.assertFalse(page.btn_event_details.isChecked())
        page._on_event_selected(parse_event({"type": "finger.enrolled"}))
        self.assertTrue(page.btn_event_details.isHidden())
        page._set_tab("recordings")
        api.recording_requests[0][0](recordings(1))
        self.assertTrue(page.event_filter_row.isHidden())
        self.assertTrue(page.event_details.isHidden())
        self.assertTrue(page.btn_event_details.isHidden())
        self.assertFalse(page.btn_play.isHidden())
        self.assertFalse(page.btn_open.isHidden())
        self.assertEqual(page._external_url, "http://localhost:8083/media/clips/20261106/0.mp4")

    def test_filter_during_loading_and_refresh_removal_clear_old_details(self):
        api = DeferredApi()
        page = RecordsPage(api)
        self.addCleanup(page.close)
        page.refresh()
        page._set_event_category("access")
        api.event_requests[0][0]([
            parse_event({"type": "finger.match"}), parse_event({"type": "intrusion"}),
        ])
        self.assertEqual(page._current_event.kind, "finger.match")
        page.refresh()
        api.event_requests[1][0]([parse_event({"type": "intrusion"})])
        self.assertTrue(page.event_details.isHidden())
        self.assertIsNone(page._current_event)
        self.assertEqual(page.video_placeholder.text(), "暂无出入记录")

    def test_filter_before_initial_response_clears_external_event_and_snapshot(self):
        api = DeferredApi()
        page = RecordsPage(api)
        self.addCleanup(page.close)
        page.refresh()
        page.play_event(parse_event({"type": "person.detected", "snapshot": "snapshots/old.jpg"}))
        page._set_event_category("access")
        self.assertTrue(page.event_details.isHidden())
        api.image_requests[0][1](image_bytes())
        self.assertIsNone(page._snapshot_pixmap)
        api.event_requests[0][0]([parse_event({"type": "finger.match"})])
        self.assertEqual(page.event_title.text(), "指纹识别成功")
        self.assertFalse(page.video_stack.isHidden())
        self.assertEqual(api.image_url_requests[0][0], "http://localhost:8083/snapshot.jpg")

    def test_external_event_reveals_its_category_and_collapsed_day(self):
        page = RecordsPage(FakeApi())
        self.addCleanup(page.close)
        page.refresh()
        event = parse_event({"type": "intrusion", "clip": "clips/selected.mp4"})
        page._on_events([event])
        page._collapsed_event_days.add("未知日期")
        page._set_event_category("package")
        page.play_event(event)
        self.assertEqual(page._event_category, "intrusion")
        self.assertFalse(page._collapsed_event_days)
        self.assertEqual(page.events_list.currentRow(), 1)
        self.assertEqual(page._external_url, "http://localhost:8083/media/clips/selected.mp4")

    def test_small_screen_layout_keeps_filters_details_and_media_in_bounds(self):
        page = RecordsPage(DeferredApi())
        self.addCleanup(page.close)
        page.setStyleSheet(theme.qss())
        page.resize(828, 496)
        page.show()
        for fields in (
            {"type": "intrusion", "clip": "clips/first.mp4"},
            {"type": "person.detected", "snapshot": "snapshots/first.jpg"},
            {"type": "finger.match", "user": "张三"},
        ):
            with self.subTest(fields=fields):
                page._on_event_selected(parse_event(fields))
                self.app.processEvents()
                self.assertEqual(page.size(), page_records_module.QSize(828, 496))
                for widget in (page.event_filter_row, page.event_details, page.video_stack,
                               page.btn_event_details, page.btn_play, page.btn_open):
                    if widget.isHidden():
                        continue
                    position = widget.mapTo(page, widget.rect().bottomRight())
                    self.assertLess(position.x(), page.width())
                    self.assertLess(position.y(), page.height())
                for button in (page.btn_event_details, page.btn_play, page.btn_open):
                    if not button.isHidden():
                        self.assertGreaterEqual(button.height(), 40)
                if not page.video_stack.isHidden():
                    self.assertGreaterEqual(page.video_stack.height(), 290)

    def test_real_video_widget_gets_larger_16_by_9_display_at_minimum_size(self):
        from PySide6.QtMultimedia import QVideoFrame

        api = FakeApi()
        api.media_url = lambda _path: ""
        with patch.object(page_records_module, "HAVE_MULTIMEDIA", True):
            page = RecordsPage(api)
            self.addCleanup(page.close)
            page.setStyleSheet(theme.qss())
            page.resize(828, 496)
            page.show()
            page._on_event_selected(parse_event({
                "type": "intrusion", "clip": "clips/event.mp4",
            }))
            frame_image = QImage(640, 360, QImage.Format.Format_RGB32)
            frame_image.fill(Qt.GlobalColor.red)
            frame = QVideoFrame(frame_image)
            self.assertTrue(frame.isValid())
            page._awaiting_video = True
            page.video.videoSink().setVideoFrame(frame)
            self.app.processEvents()

            self.assertEqual(page.size(), page_records_module.QSize(828, 496))
            self.assertIs(page.video_stack.currentWidget(), page.video)
            self.assertEqual(page.video.size(), page.video_stack.size())
            self.assertEqual(page.video.aspectRatioMode(), Qt.AspectRatioMode.KeepAspectRatio)
            fitted_height = min(page.video.height(), page.video.width() * 9 / 16)
            self.assertGreaterEqual(fitted_height, 250)
            self.assertGreaterEqual(fitted_height * 16 / 9, 440)

    def test_live_preview_covers_wide_viewport_without_side_letterbox(self):
        view = VideoView(FakeApi())
        self.addCleanup(view.close)
        view.resize(800, 400)
        view._state = "live"
        view._pixmap = QImage(1280, 720, QImage.Format.Format_RGB32)
        view._apply()

        pixmap = view.pixmap()
        self.assertEqual(view.alignment(), Qt.AlignmentFlag.AlignCenter)
        self.assertEqual(pixmap.width(), view.width())
        self.assertGreater(pixmap.height(), view.height())


if __name__ == "__main__":
    unittest.main()