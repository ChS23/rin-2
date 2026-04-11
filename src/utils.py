import json
import os
import tempfile
from pathlib import Path


def safe_json_write(path: Path, data, **kwargs):
    """Crash-safe JSON write: temp file + atomic rename"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, **kwargs)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise
