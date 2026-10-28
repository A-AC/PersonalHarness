"""Structured logging: plain lines to stdout and a rotating file under data/.

Every important state transition also lands in the ``event_log`` table (see
``store.Events``); this module is only the developer/operator console trail.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING,
            "warning": logging.WARNING, "error": logging.ERROR}

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_configured = False


def setup_logging(data_dir: Path, level: str = "info",
                  file_name: str = "logs/harness.log") -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger("harness")
    root.setLevel(_LEVELS.get(level.lower(), logging.INFO))
    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        log_path = data_dir / file_name
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:  # read-only data dir: console only
        pass
    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"harness.{name}")
