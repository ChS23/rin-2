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


async def run_agent_streamed(agent, input, *, hooks=None, run_config=None, max_turns=10):
    """Запустить агента в streaming режиме (stream=true к API), вернуть финальный результат.
    Z.AI coding plan ожидает streaming запросы от coding tools."""
    from agents import Runner

    result = Runner.run_streamed(
        agent, input, hooks=hooks, run_config=run_config, max_turns=max_turns,
    )
    async for _event in result.stream_events():
        pass  # consume stream, ждём завершения
    return result
