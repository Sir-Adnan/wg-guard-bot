"""Logging setup: human-friendly in development, JSON-ish lines in production."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False

_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color = _COLORS.get(record.levelname, "")
        record.levelname_colored = f"{color}{record.levelname:<8}{_RESET}"
        return super().format(record)


def setup_logging(level: str = "INFO", *, log_dir: Path | None = None, json_lines: bool = False) -> None:
    """Configure root logging once per process."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if json_lines:
        fmt = '{"ts":"%(asctime)s","lvl":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
        formatter: logging.Formatter = logging.Formatter(fmt, datefmt="%Y-%m-%dT%H:%M:%S%z")
    else:
        fmt = "%(asctime)s | %(levelname_colored)s | %(name)-28s | %(message)s"
        formatter = _ColorFormatter(fmt, datefmt="%H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
            root.addHandler(file_handler)
        except OSError:  # pragma: no cover - read-only FS
            pass

    # Noise reduction for chatty third parties.
    for noisy in ("aiogram.event", "apscheduler.executors.default", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("aiogram.dispatcher").setLevel(logging.INFO)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
