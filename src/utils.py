import os
import tempfile
from pathlib import Path

import orjson


def safe_json_write(path: Path, data):
    """Crash-safe JSON write: temp file + atomic rename"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(orjson.dumps(data, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS))
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise
