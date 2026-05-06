"""Persistent data directory resolution.

Priority: BOT_DATA_DIR -> RAILWAY_VOLUME_MOUNT_PATH -> ./data
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional


def _resolve_data_dir() -> tuple[str, str]:
    for key in ("BOT_DATA_DIR", "RAILWAY_VOLUME_MOUNT_PATH"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value, key
    return "data", "default"


DATA_DIR_RAW, DATA_DIR_SOURCE = _resolve_data_dir()
DATA_DIR = Path(DATA_DIR_RAW).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.sqlite3"


def probe_path_writable(path: Path) -> tuple[bool, Optional[str]]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        return True, None
    except OSError as exc:
        return False, str(exc)


def log_storage_diagnostics(logger: logging.Logger) -> list[str]:
    issues: list[str] = []
    abs_data = DATA_DIR.resolve()
    logger.info(
        "[startup] DATA_DIR=%s source=%s raw=%r cwd=%s",
        abs_data,
        DATA_DIR_SOURCE,
        DATA_DIR_RAW,
        Path.cwd().resolve(),
    )

    ok, err = probe_path_writable(DATA_DIR)
    if ok:
        logger.info("[startup] DATA_DIR write probe: OK")
    else:
        message = f"DATA_DIR write failed: {err}"
        logger.error("[startup] %s", message)
        issues.append(message)

    railway = bool((os.environ.get("RAILWAY_ENVIRONMENT") or "").strip())
    if railway and DATA_DIR_SOURCE == "default":
        message = (
            "Railway volume is not confirmed. Add a Volume to this service with "
            "mount path /app/data, or set BOT_DATA_DIR=/app/data."
        )
        logger.warning("[startup] %s", message)
        issues.append(message)
    elif DATA_DIR_SOURCE == "RAILWAY_VOLUME_MOUNT_PATH":
        logger.info("[startup] Railway volume mount path is used for DATA_DIR.")

    return issues
