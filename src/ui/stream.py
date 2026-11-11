"""MJPEG 预览控件。

为什么不用 QNetworkAccessManager 直接在 UI 线程里解析：
640×360 JPEG 在 RK3576 上解码约 2–4 ms，10 fps 看似没问题，但一旦遇到
1080p 或大客户端数量的瞬间，掉帧会直接卡住整个界面（包括开门按钮）。
所以解析 + 解码放在专属线程里，UI 线程只负责贴图。

断流处理：软件重启、相机重插、visiond 重启都会断。这里做指数退避重连，
并把"最后一次成功收帧时间"暴露给上层，用于显示"信号中断"而不是静默黑屏。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication, QMetaObject, QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QLabel

from . import theme

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
MAX_STREAM_BUFFER = 8 * 1024 * 1024
MAX_JPEG_SIZE = 2 * 1024 * 1024


class _StreamWorker(QObject):
    """跑在自己的线程里，拥有独立的 QNetworkAccessManager。"""

    frame = Signal(object)
    state = Signal(str)          # connecting / live / error

    def __init__(self, url: str, token: str = "") -> None:
        super().__init__()
        self._url = url
        self._token = token
        self._nam: QNetworkAccessManager | None = None
        self._reply: QNetworkReply | None = None
        self._buf = b""
        self._stopped = False

    @Slot()
    def start(self) -> None:
        self._nam = QNetworkAccessManager(self)
        self._connect()

    @Slot()
    def stop(self) -> None:
        self._stopped = True
        self._abort()

    def _abort(self) -> None:
        if self._reply is not None:
            self._reply.abort()
            self._reply.deleteLater()
            self._reply = None

    def _connect(self) -> None:
        if self._stopped or self._nam is None:
            return
        self.state.emit("connecting")
        req = QNetworkRequest(self._url)
        # MJPEG 是无限长响应，不能设传输总超时，只能靠"多久没收到数据"判断断流。
        req.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                         QNetworkRequest.CacheLoadControl.AlwaysNetwork)
        if self._token:
            req.setRawHeader(b"Authorization", f"Bearer {self._token}".encode())
        self._reply = self._nam.get(req)
        self._reply.readyRead.connect(self._on_ready)
        self._reply.errorOccurred.connect(self._on_error)
        self._buf = b""

    @Slot()
    def _on_ready(self) -> None:
        if self._reply is None:
            return
        chunk = bytes(self._reply.readAll().data())
        if not chunk:
            return
        self._buf += chunk
        # 保护上限：避免异常流导致缓冲无限增长触发内存/Qt 层不稳定。
        if len(self._buf) > MAX_STREAM_BUFFER:
            last_soi = self._buf.rfind(SOI)
            self._buf = self._buf[last_soi:] if last_soi >= 0 else b""

        got = False
        last_jpg: bytes | None = None
        while True:
            start = self._buf.find(SOI)
            if start < 0:
                self._buf = b""
                break
            end = self._buf.find(EOI, start + 2)
            if end < 0:
                self._buf = self._buf[start:]
                break
            jpg = self._buf[start:end + 2]
            self._buf = self._buf[end + 2:]
            if len(jpg) > MAX_JPEG_SIZE:
                continue
            last_jpg = jpg
            got = True

        # 每次 readyRead 只投递最后一帧，减少跨线程队列积压。
        if last_jpg is not None:
            self.frame.emit(last_jpg)
        if got:
            self.state.emit("live")

    @Slot()
    def _on_error(self) -> None:
        if self._stopped:
            return
        self.state.emit("error")
        self._abort()
        # 由外层 QTimer 触发重连，避免在信号槽里递归建连
        QTimer.singleShot(2000, self._connect)


class VideoView(QLabel):
    """显示 MJPEG 流；没有流时显示提示文字而不是黑屏。"""

    def __init__(self, api, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Video")
        self.setMinimumSize(560, 315)
        self.setScaledContents(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._api = api
        self._thread: QThread | None = None
        self._worker: _StreamWorker | None = None
        self._stopping = False
        self._last_frame = 0.0
        self._state = "idle"
        self._pixmap = None
        self._placeholder = "正在连接摄像头…"
        self._set_placeholder()

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._check_stale)
        self._watchdog.start()

        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping = False
        self._thread = QThread(self)
        self._worker = _StreamWorker(self._api.stream_url(), self._api.token)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self._thread.finished.connect(self._worker.deleteLater)
        self._worker.frame.connect(self._on_frame)
        self._worker.state.connect(self._on_state)
        self._thread.start()

    def stop(self) -> None:
        """先让 worker 断流，再退出线程。

        顺序很重要：worker 的 QNetworkAccessManager 属于子线程，
        线程先退出而 reply 还活着，析构时会在错误线程上删对象。
        """
        if self._stopping:
            return
        self._stopping = True

        worker = self._worker
        thread = self._thread

        if worker is not None and thread is not None and thread.isRunning():
            # 异步投递 stop，避免 PySide 在 BlockingQueuedConnection 上的析构竞态。
            QMetaObject.invokeMethod(
                worker,
                "stop",
                Qt.ConnectionType.QueuedConnection,
            )

        if thread is not None:
            thread.quit()
            thread.wait(3000)
            thread.deleteLater()
            self._thread = None

        if worker is not None:
            try:
                worker.frame.disconnect(self._on_frame)
            except (TypeError, RuntimeError):
                pass
            try:
                worker.state.disconnect(self._on_state)
            except (TypeError, RuntimeError):
                pass
        self._worker = None
        self._pixmap = None
        self._stopping = False

    # ---- 槽 ----
    @Slot(object)
    def _on_frame(self, jpg: object) -> None:
        if not isinstance(jpg, (bytes, bytearray, memoryview)):
            return
        payload = bytes(jpg)
        img = QImage.fromData(payload, "JPG")
        if img.isNull():
            return
        self._last_frame = time.monotonic()
        self._state = "live"
        self._pixmap = img.copy()
        self._apply()

    @Slot(str)
    def _on_state(self, state: str) -> None:
        self._state = state
        if state == "connecting":
            self._placeholder = "正在连接摄像头…"
        elif state == "error":
            self._placeholder = "摄像头信号中断，正在重连…"
        if state != "live":
            self._apply()

    def _check_stale(self) -> None:
        if self._state != "live":
            return
        if time.monotonic() - self._last_frame > 5.0:
            self._state = "stale"
            self._placeholder = "画面已停止更新（超过 5 s）"
            self._apply()

    # ---- 绘制 ----
    def _set_placeholder(self) -> None:
        self.setPixmap(self._empty_pixmap(self._placeholder))

    def _apply(self) -> None:
        if self._pixmap is None or self._state != "live":
            self.setPixmap(self._empty_pixmap(self._placeholder))
            return
        size = self.size()
        if size.width() < 2 or size.height() < 2:
            return      # 布局还没跑过，此时 scaled() 会返回 null
        # QImage.scaled() 返回 QImage，QLabel.setPixmap 需要 QPixmap，必须显式转换
        self.setPixmap(QPixmap.fromImage(self._pixmap).scaled(
            size, _cover, _fast,
        ))

    def _empty_pixmap(self, text: str):
        pm = QPixmap(max(1, self.width()), max(1, self.height()))
        pm.fill(QColor("#05070A"))
        if self.width() < 40 or self.height() < 20:
            return pm       # 尺寸太小就不画字，避免 painter 在 0 尺寸上出问题
        painter = QPainter(pm)
        painter.setPen(QColor(theme.TEXT_MUTED))
        font = painter.font()
        font.setPixelSize(15)
        painter.setFont(font)
        painter.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
        return pm

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self._apply()


_cover = Qt.AspectRatioMode.KeepAspectRatioByExpanding
_fast = Qt.TransformationMode.FastTransformation
