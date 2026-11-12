"""visiond / audiood 的 HTTP 客户端（Qt 原生异步，绝不阻塞 UI 线程）。

设计要点
--------
* 全部走 QNetworkAccessManager：不引入 requests/httpx 到 GUI 进程，
  也不需要在 QThread 里做同步 IO，回调直接落在事件循环上。
* 每个请求都带超时（默认 5 s）。UI 进程最怕的不是报错，是卡住。
* 媒体地址统一在这里生成，业务代码不拼 URL —— 换端口/换鉴权方式只改一处。
"""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QObject, QUrl, QUrlQuery, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from . import theme

# 事件分类：记录中心与事件页共用
CATEGORY_OF = {
    "person.detected": "intrusion",
    "intrusion": "intrusion",
    "package.detected": "package",
    "package.picked": "package",
    "finger.match": "access",
    "finger.reject": "access",
    "finger.enrolled": "system",
    "finger.deleted": "system",
    "finger.renamed": "system",
    "door.opened": "access",
    "door.closed": "access",
    "unlock.remote": "access",
    "unlock.request": "access",
    "tamper": "intrusion",
}

CATEGORIES: list[tuple[str, str]] = [
    ("all", "全部"),
    ("intrusion", "安全"),
    ("access", "出入"),
    ("package", "快递"),
    ("system", "系统"),
]

# 事件详情里值得直接展示的字段（顺序即展示顺序）
DETAIL_LABELS: dict[str, str] = {
    "dwell_s": "停留",
    "night": "夜间",
    "object": "识别为",
    "static_s": "静止",
    "person_left_s": "人离开",
    "bbox": "位置",
    "method": "方式",
    "user": "用户",
    "score": "匹配度",
    "finger_id": "指纹 ID",
    "finger": "手指",
    "reason": "原因",
    "decision_text": "处理",
    "text": "访客原话",
}


def _fmt_duration(value: Any) -> str:
    try:
        return f"{float(value):.0f} s"
    except (TypeError, ValueError):
        return str(value)


@dataclass(slots=True)
class Event:
    ts: datetime | None
    kind: str
    severity: str
    conf: float
    zone: str
    snapshot: str | None
    clip: str | None
    detail: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        return theme.EVENT_TITLE.get(self.kind, self.kind)

    @property
    def category(self) -> str:
        return CATEGORY_OF.get(self.kind, "system")

    @property
    def color(self) -> str:
        return theme.SEVERITY_COLOR.get(self.severity, theme.INFO)

    @property
    def time_text(self) -> str:
        return self.ts.strftime("%H:%M:%S") if self.ts else "--:--:--"

    @property
    def date_text(self) -> str:
        return self.ts.strftime("%m-%d") if self.ts else "--"

    def summary(self) -> str:
        """一行副标题：优先说人话，再补数字。"""
        d = self.detail
        if self.kind == "intrusion":
            return f"门前停留 {_fmt_duration(d.get('dwell_s', 0))}"
        if self.kind == "person.detected":
            return f"置信度 {self.conf:.2f}"
        if self.kind == "package.detected":
            return f"{d.get('object', '物体')} 静止 {_fmt_duration(d.get('static_s', 0))}"
        if self.kind == "unlock.request" and "decision" in d:
            said = str(d.get("text") or "").strip()
            result = str(d.get("decision_text") or d.get("decision"))
            if d.get("decision") == "allow" and d.get("unlocked") is False:
                result += "（未开锁）"
            return f"{result} · “{said}”" if said else result
        if self.kind in ("finger.match", "unlock.remote", "unlock.request"):
            return f"{d.get('method', '指纹')} · {d.get('user', '主人')} · ID {d.get('finger_id', '--')}"
        if self.kind == "finger.reject":
            return str(d.get("reason") or "指纹未匹配")
        if self.kind in ("finger.enrolled", "finger.deleted", "finger.renamed"):
            return f"{d.get('user', '用户')} · ID {d.get('finger_id', '--')}"
        if self.kind == "tamper":
            return "面板防拆开关触发"
        return f"置信度 {self.conf:.2f}"

    def detail_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for key, label in DETAIL_LABELS.items():
            if key not in self.detail:
                continue
            value = self.detail[key]
            if key == "night":
                value = "是" if value else "否"
            elif key.endswith("_s"):
                value = _fmt_duration(value)
            elif key == "bbox":
                try:
                    value = " ".join(f"{int(v)}" for v in value)
                except (TypeError, ValueError):
                    pass
            rows.append((label, str(value)))
        rows.append(("来源", str(self.raw.get("source", "visiond"))))
        return rows


def parse_event(raw: dict) -> Event:
    ts: datetime | None = None
    value = raw.get("ts")
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value)
        except ValueError:
            ts = None
    elif isinstance(value, (int, float)):
        ts = datetime.fromtimestamp(value).astimezone()

    skip = {"ts", "type", "source", "severity", "conf", "zone", "snapshot", "clip"}
    detail = {k: v for k, v in raw.items() if k not in skip}
    return Event(
        ts=ts,
        kind=str(raw.get("type", "event")),
        severity=str(raw.get("severity", "info")),
        conf=float(raw.get("conf") or 0.0),
        zone=str(raw.get("zone", "doorstep")),
        snapshot=raw.get("snapshot"),
        clip=raw.get("clip"),
        detail=detail,
        raw=raw,
    )


@dataclass(slots=True)
class Recording:
    path: str
    kind: str
    ts: datetime | None = None
    size: int = 0
    event_type: str | None = None
    severity: str | None = None
    snapshot: str | None = None

    @property
    def date_text(self) -> str:
        return self.ts.strftime("%m-%d") if self.ts else "--"

    @property
    def time_text(self) -> str:
        return self.ts.strftime("%H:%M:%S") if self.ts else "--:--:--"

    @property
    def kind_text(self) -> str:
        return "事件片段" if self.kind == "event" else "在线录像"

    @property
    def title(self) -> str:
        if self.event_type:
            return theme.EVENT_TITLE.get(self.event_type, self.event_type)
        return self.kind_text

    @property
    def color(self) -> str:
        sev = self.severity or "info"
        return theme.SEVERITY_COLOR.get(sev, theme.INFO)


def parse_recording(raw: dict) -> Recording:
    ts: datetime | None = None
    value = raw.get("ts")
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value)
        except ValueError:
            ts = None
    elif isinstance(value, (int, float)):
        ts = datetime.fromtimestamp(value).astimezone()

    return Recording(
        path=str(raw.get("path") or ""),
        kind=str(raw.get("kind") or "event"),
        ts=ts,
        size=int(raw.get("size") or 0),
        event_type=raw.get("event_type"),
        severity=raw.get("severity"),
        snapshot=raw.get("snapshot"),
    )


@dataclass(slots=True)
class Health:
    ok: bool = False
    fps: float = 0.0
    ring: int = 0
    preview_clients: int = 0
    detector: str | None = None
    detector_error: str | None = None
    package_detector: str | None = None
    package_detector_enabled: bool = False
    package_detector_error: str | None = None
    clips_active: int = 0
    recording_requested: bool = False
    recording_active: bool = False
    error: str = ""

    @classmethod
    def from_json(cls, data: dict) -> "Health":
        return cls(
            ok=bool(data.get("ok")),
            fps=float(data.get("fps") or 0.0),
            ring=int(data.get("ring") or 0),
            preview_clients=int(data.get("preview_clients") or 0),
            detector=data.get("detector"),
            detector_error=data.get("detector_error"),
            package_detector=data.get("package_detector"),
            package_detector_enabled=bool(data.get("package_detector_enabled")),
            package_detector_error=data.get("package_detector_error"),
            clips_active=int(data.get("clips_active") or 0),
            recording_requested=bool((data.get("recording") or {}).get("requested")),
            recording_active=bool((data.get("recording") or {}).get("active")),
        )


class ApiClient(QObject):
    """一个实例全进程共用：QNetworkAccessManager 自身带连接池。"""

    def __init__(self, base_url: str, token: str = "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._nam = QNetworkAccessManager(self)

    # ---------------- URL ----------------
    def url(self, path: str, params: dict[str, Any] | None = None) -> QUrl:
        url = QUrl(self.base_url + path)
        query = QUrlQuery()
        for key, value in (params or {}).items():
            query.addQueryItem(key, str(value))
        if self.token:
            query.addQueryItem("token", self.token)
        if not query.isEmpty():
            url.setQuery(query)
        return url

    # 事件里只可能出现这两类媒体目录；按它切分比猜前缀可靠
    MEDIA_ROOTS = ("snapshots", "clips")

    def media_url(self, path: str | None) -> str | None:
        """把事件里的路径转成 /media/ 可下载地址。

        visiond 落库时写的是**相对进程 cwd** 的路径，可能是
        `data/snapshots/x.jpg`、`data/ui/clips/x.mp4`，data_dir 里也可能
        配置成绝对路径。而 `/media/{path}` 是相对 data_dir 的。

        所以不做前缀字符串比较，直接定位到 `snapshots/` 或 `clips/` 这一段：
        只要 data_dir 的配置与 visiond 一致，拼接出来必然正确。
        """
        if not path:
            return None
        rel = str(path).replace("\\", "/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        for i, seg in enumerate(parts):
            if seg in self.MEDIA_ROOTS:
                rel = "/".join(parts[i:])
                break
        else:
            rel = "/".join(p for p in parts if p != "..")
        return self.url("/media/" + urllib.parse.quote(rel, safe="/")).toString()

    # ---------------- 请求 ----------------
    def _request(self, req: QNetworkRequest) -> QNetworkReply:
        req.setTransferTimeout(5000)
        req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                         QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        if self.token:
            req.setRawHeader(b"Authorization", f"Bearer {self.token}".encode())
        return self._nam.get(req)

    def get_json(
        self,
        path: str,
        on_ok: Callable[[Any], None],
        on_err: Callable[[str], None] | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        reply = self._request(QNetworkRequest(self.url(path, params)))
        reply.finished.connect(lambda: self._finish_json(reply, on_ok, on_err))

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        req = QNetworkRequest(self.url(path))
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        req.setTransferTimeout(5000)
        if self.token:
            req.setRawHeader(b"Authorization", f"Bearer {self.token}".encode())
        body = json.dumps(payload).encode()
        reply = self._nam.post(req, body)
        reply.finished.connect(lambda: self._finish_json(reply, on_ok, on_err))

    @staticmethod
    def _finish_json(
        reply: QNetworkReply,
        on_ok: Callable[[Any], None] | None,
        on_err: Callable[[str], None] | None,
    ) -> None:
        try:
            status = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            data = bytes(reply.readAll().data())
            if reply.error() != QNetworkReply.NetworkError.NoError or not status or status >= 400:
                message = f"HTTP {status}" if status else reply.errorString()
                if data:
                    try:
                        detail = data.decode("utf-8", errors="replace").strip()
                        if detail:
                            message = f"{message}: {detail}"
                    except Exception:  # noqa: BLE001
                        pass
                if on_err:
                    on_err(message)
                return
            payload = json.loads(data.decode("utf-8")) if data else None
            if on_ok:
                on_ok(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            if on_err:
                on_err(str(exc))
        finally:
            reply.deleteLater()

    def get_bytes_url(
        self,
        url: str,
        on_ok: Callable[[bytes], None],
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        reply = self._request(QNetworkRequest(QUrl(url)))

        def done() -> None:
            try:
                data = bytes(reply.readAll().data())
                if reply.error() != QNetworkReply.NetworkError.NoError or not data:
                    if on_err:
                        on_err(reply.errorString())
                    return
                on_ok(data)
            finally:
                reply.deleteLater()

        reply.finished.connect(done)

    def get_bytes(
        self,
        path: str,
        on_ok: Callable[[bytes], None],
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        url = self.media_url(path)
        if not url:
            if on_err:
                on_err("无路径")
            return
        self.get_bytes_url(url, on_ok, on_err)

    # ---------------- 语义接口 ----------------
    def health(self, on_ok: Callable[[Health], None], on_err: Callable[[str], None] | None = None) -> None:
        self.get_json("/health", lambda d: on_ok(Health.from_json(d or {})), on_err)

    def audio_health(self, on_ok: Callable[[Any], None], on_err: Callable[[str], None] | None = None) -> None:
        url = self.url("/health")
        url.setPort(int(os.getenv("AUDIO_HTTP_PORT", "8084")))
        reply = self._request(QNetworkRequest(url))
        reply.finished.connect(lambda: self._finish_json(reply, on_ok, on_err))

    def audio_unlock_decision(
        self,
        payload: dict[str, Any],
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        """POST audiood /unlock_request：把屋主对访客开门请求的决定告诉语音服务。"""
        url = self.url("/unlock_request")
        url.setPort(int(os.getenv("AUDIO_HTTP_PORT", "8084")))
        request = QNetworkRequest(url)
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setTransferTimeout(5000)
        reply = self._nam.post(request, json.dumps(payload).encode())
        reply.finished.connect(lambda: self._finish_json(reply, on_ok, on_err))

    def _fingerprint_url(self, path: str) -> QUrl:
        port = int(os.getenv("FINGERPRINT_HTTP_PORT", "8085"))
        return QUrl(f"http://127.0.0.1:{port}{path}")

    def _lock_url(self, path: str) -> QUrl:
        port = int(os.getenv("LOCK_HTTP_PORT", "8086"))
        return QUrl(f"http://127.0.0.1:{port}{path}")

    def lock_health(
        self,
        on_ok: Callable[[Any], None],
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        request = QNetworkRequest(self._lock_url("/health"))
        request.setTransferTimeout(3000)
        reply = self._nam.get(request)
        reply.finished.connect(lambda: self._finish_json(reply, on_ok, on_err))

    def lock_unlock(
        self,
        payload: dict[str, Any] | None = None,
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        """POST /lock/unlock：界面只发请求，开不开由 lockd 的策略决定。"""
        request = QNetworkRequest(self._lock_url("/unlock"))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        body = json.dumps({"source": "ui", **(payload or {})}).encode()
        request.setTransferTimeout(15000)
        reply = self._nam.post(request, body)
        reply.finished.connect(lambda: self._finish_json(reply, on_ok, on_err))

    def fingerprint_get(
        self,
        path: str,
        on_ok: Callable[[Any], None],
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        request = QNetworkRequest(self._fingerprint_url(path))
        request.setTransferTimeout(5000)
        reply = self._nam.get(request)
        reply.finished.connect(lambda: self._finish_json(reply, on_ok, on_err))

    def fingerprint_post(
        self,
        path: str,
        payload: dict[str, Any],
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        request = QNetworkRequest(self._fingerprint_url(path))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setTransferTimeout(5000)
        reply = self._nam.post(request, json.dumps(payload).encode())
        reply.finished.connect(lambda: self._finish_json(reply, on_ok, on_err))

    def fingerprint_health(
        self,
        on_ok: Callable[[Any], None],
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        self.fingerprint_get("/health", on_ok, on_err)

    def fingerprint_templates(
        self,
        on_ok: Callable[[list[dict]], None],
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        self.fingerprint_get(
            "/templates",
            lambda data: on_ok((data or {}).get("items", [])),
            on_err,
        )

    def fingerprint_enroll(
        self, name: str, finger: str,
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        self.fingerprint_post("/enroll", {"name": name, "finger": finger}, on_ok, on_err)

    def fingerprint_refresh(
        self,
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        self.fingerprint_post("/refresh", {}, on_ok, on_err)

    def fingerprint_delete(
        self, location: int,
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        self.fingerprint_post("/delete", {"id": location}, on_ok, on_err)

    def fingerprint_rename(
        self, location: int, name: str, finger: str,
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        self.fingerprint_post(
            "/rename", {"id": location, "name": name, "finger": finger}, on_ok, on_err
        )

    def fingerprint_cancel(
        self,
        on_ok: Callable[[Any], None] | None = None,
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        self.fingerprint_post("/cancel", {}, on_ok, on_err)

    def events(self, limit: int = 1000, on_ok: Callable[[list[Event]], None] | None = None,
               on_err: Callable[[str], None] | None = None) -> None:
        def handle(data: Any) -> None:
            items = (data or {}).get("items") if isinstance(data, dict) else data
            events = [parse_event(x) for x in (items or []) if isinstance(x, dict)]
            if on_ok:
                on_ok(events)

        self.get_json("/events", handle, on_err, {"limit": limit})

    def recordings(
        self,
        limit: int = 200,
        kind: str = "all",
        on_ok: Callable[[list[Recording]], None] | None = None,
        on_err: Callable[[str], None] | None = None,
    ) -> None:
        def handle(data: Any) -> None:
            items = (data or {}).get("items") if isinstance(data, dict) else data
            records = [parse_recording(x) for x in (items or []) if isinstance(x, dict)]
            if on_ok:
                on_ok(records)

        self.get_json("/recordings", handle, on_err, {"limit": limit, "type": kind})

    def stream_url(self) -> str:
        return self.url("/stream").toString()

    def snapshot_url(self) -> str:
        return self.url("/snapshot.jpg").toString()

    # ---------------- 门锁、对讲与录像控制 ----------------
    def unlock(self, on_ok: Callable[[Any], None] | None = None,
               on_err: Callable[[str], None] | None = None) -> None:
        """POST /unlock —— 由 lockd 落地，UI 只发请求不持有钥匙。"""
        self.post_json("/unlock", {"source": "ui"}, on_ok, on_err)

    def talk(self, on: bool, on_ok: Callable[[Any], None] | None = None,
             on_err: Callable[[str], None] | None = None) -> None:
        """POST /talk {on} —— 由 audiood 落地，音频永远不经过 UI 进程。"""
        self.post_json("/talk", {"on": on}, on_ok, on_err)

    def recording(self, on: bool, on_ok: Callable[[Any], None] | None = None,
                  on_err: Callable[[str], None] | None = None) -> None:
        """POST /recording {on}：启停事件录像之外的分段在线录像。"""
        self.post_json("/recording", {"on": on}, on_ok, on_err)

    def ui_heartbeat(self, client: str, active: bool = True,
                     on_ok: Callable[[Any], None] | None = None,
                     on_err: Callable[[str], None] | None = None) -> None:
        """POST /ui/heartbeat：续租让 visiond 保持检测；active=False 立即释放。"""
        self.post_json("/ui/heartbeat", {"client": client, "active": active}, on_ok, on_err)


class CapabilityProbe(QObject):
    """探测 lockd/audiood 的 /unlock、/talk 是否可达。

    后端未运行或不可达时按钮保持可见但置灰，便于用户区分后端未启动和界面故障。
    """

    changed = Signal(bool, bool)   # unlock_ok, talk_ok

    def __init__(self, api: ApiClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self.unlock_ok = False
        self.talk_ok = False

    def probe(self) -> None:
        self._probe_one("/unlock", "unlock_ok")
        self._probe_one("/talk", "talk_ok")

    def _probe_one(self, path: str, attr: str) -> None:
        # GET 到一个只接受 POST 的接口会返回 405，说明接口存在。
        reply = self.api._request(QNetworkRequest(self.api.url(path)))

        def done() -> None:
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            setattr(self, attr, status is not None and status != 404)
            reply.deleteLater()
            self.changed.emit(self.unlock_ok, self.talk_ok)

        reply.finished.connect(done)


def _guess_data_dir(absolute_path: str) -> str:
    """从绝对路径反推 data_dir：取 snapshots/clips 的上一级。"""
    parts = absolute_path.split(os.sep)
    for marker in ("snapshots", "clips"):
        if marker in parts:
            idx = parts.index(marker)
            return os.sep.join(parts[:idx]) or os.sep
    return os.path.dirname(absolute_path)
