"""Small persistent state for Doc mode, kept as files under STATE_DIR.

- state.json: hashes of the last raw input and the last output written to the Doc
- backups/: the last few raw scripts read from the Doc, before any change

On Railway, STATE_DIR must be a mounted volume to survive restarts.
"""

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from bot import config

logger = logging.getLogger(__name__)


def text_hash(text: str) -> str:
    """Hash after trimming and collapsing whitespace / line-ending differences only."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def _state_path() -> Path:
    return Path(config.STATE_DIR) / "state.json"


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(data)
    os.replace(tmp, path)


def load() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.exception("Couldn't read %s - starting with empty state", _state_path())
        return {}


def update(**values: str) -> None:
    state = load()
    state.update(values)
    _atomic_write(_state_path(), json.dumps(state, indent=2))


def save_backup(text: str, prefix: str = "backup") -> Path:
    """Save a copy under STATE_DIR/backups, keeping only the newest BACKUPS_KEEP per prefix."""
    folder = Path(config.STATE_DIR) / "backups"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = folder / f"{prefix}_{stamp}.txt"
    _atomic_write(path, text)
    for old in sorted(folder.glob(f"{prefix}_*.txt"))[: -config.BACKUPS_KEEP]:
        old.unlink(missing_ok=True)
    return path
