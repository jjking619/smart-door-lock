"""Logging setup for audiood with local rotating log files."""

from __future__ import annotations

from datetime import datetime
import logging
import os
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _parse_level(name: str) -> int:
    level = getattr(logging, (name or "INFO").upper(), None)
    return level if isinstance(level, int) else logging.INFO


def _resolve_dir(path_value: str, default_rel: str) -> Path:
    value = path_value.strip() or default_rel
    base = Path(value).expanduser()
    if not base.is_absolute():
        base = Path.cwd() / base
    return base


def configure_audio_logging(service_name: str = "audiood") -> Path | None:
    """Configure console + rotating file handlers.

    Env vars:
      LOG_LEVEL               default INFO
      AUDIO_LOG_DIR           default data/logs
      AUDIO_LOG_FILE          default audiood.log
      AUDIO_LOG_MAX_MB        default 10
      AUDIO_LOG_BACKUP_COUNT  default 5
      AUDIO_LOG_CONSOLE       default 1 (0 disables console output)
    """

    level = _parse_level(os.getenv("LOG_LEVEL", "INFO"))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    handlers: list[logging.Handler] = []
    log_path: Path | None = None

    console_enabled = os.getenv("AUDIO_LOG_CONSOLE", "1") != "0"
    if console_enabled:
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(fmt)
        handlers.append(console)

    try:
        log_dir = _resolve_dir(os.getenv("AUDIO_LOG_DIR", "data/logs"), "data/logs")
        log_dir.mkdir(parents=True, exist_ok=True)

        log_file_value = os.getenv("AUDIO_LOG_FILE", f"{service_name}.log").strip() or f"{service_name}.log"
        raw_path = Path(log_file_value).expanduser()
        log_path = raw_path if raw_path.is_absolute() else (log_dir / raw_path)

        max_mb = float(os.getenv("AUDIO_LOG_MAX_MB", "10"))
        max_bytes = max(int(max_mb * 1024 * 1024), 256 * 1024)
        backups = max(int(os.getenv("AUDIO_LOG_BACKUP_COUNT", "5")), 1)

        file_handler = RotatingFileHandler(
            filename=log_path,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(fmt)
        handlers.append(file_handler)
    except Exception as exc:  # noqa: BLE001
        fallback = logging.StreamHandler()
        fallback.setLevel(level)
        fallback.setFormatter(fmt)
        logging.basicConfig(level=level, handlers=[fallback], force=True)
        logging.getLogger(service_name).warning("file logging disabled: %s", exc)
        return None

    logging.basicConfig(level=level, handlers=handlers, force=True)
    logger = logging.getLogger(service_name)
    logger.info(
        "logging configured level=%s file=%s console=%s",
        logging.getLevelName(level),
        str(log_path) if log_path else "disabled",
        console_enabled,
    )
    return log_path


def create_session_file_logger(
    service_name: str,
    session_id: str,
) -> tuple[logging.Logger | None, Path | None, Callable[[], None]]:
    """Create one dedicated log file for a single dialog session.

    Env vars:
      AUDIO_SESSION_LOG_ENABLED    default 1
      AUDIO_SESSION_LOG_DIR        default data/logs/sessions
      AUDIO_SESSION_LOG_LAYOUT     default date (date|flat)
      AUDIO_SESSION_LOG_FILE_TEMPLATE default session-{sid}.log
      AUDIO_SESSION_LOG_LEVEL      default LOG_LEVEL
    """

    if os.getenv("AUDIO_SESSION_LOG_ENABLED", "1") == "0":
        return None, None, lambda: None

    try:
        base_dir = _resolve_dir(os.getenv("AUDIO_SESSION_LOG_DIR", "data/logs/sessions"), "data/logs/sessions")
        layout = os.getenv("AUDIO_SESSION_LOG_LAYOUT", "date").strip().lower() or "date"
        if layout == "date":
            target_dir = base_dir / datetime.now().strftime("%Y-%m-%d")
        else:
            target_dir = base_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        template = os.getenv("AUDIO_SESSION_LOG_FILE_TEMPLATE", "session-{sid}.log").strip() or "session-{sid}.log"
        filename = template.format(sid=session_id)
        path = target_dir / filename

        level = _parse_level(os.getenv("AUDIO_SESSION_LOG_LEVEL", os.getenv("LOG_LEVEL", "INFO")))
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

        logger_name = f"{service_name}.session.{session_id}"
        slogger = logging.getLogger(logger_name)
        slogger.setLevel(level)
        slogger.propagate = False

        for handler in list(slogger.handlers):
            slogger.removeHandler(handler)
            handler.close()

        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(fmt)
        slogger.addHandler(file_handler)

        def close_logger() -> None:
            for handler in list(slogger.handlers):
                slogger.removeHandler(handler)
                handler.close()

        return slogger, path, close_logger
    except Exception:
        return None, None, lambda: None
