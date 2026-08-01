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
    Z.AI coding plan ожидает streaming запросы от coding tools.

    Единственная точка входа ВСЕХ агентов — здесь же пишется даталог каждого вызова модели."""
    import time
    from agents import Runner

    started = time.monotonic()
    result = None
    err = None
    try:
        result = Runner.run_streamed(
            agent, input, hooks=hooks, run_config=run_config, max_turns=max_turns,
        )
        async for _event in result.stream_events():
            pass  # consume stream, ждём завершения
        return result
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        raise
    finally:
        try:  # даталог не должен ронять бота ни при каких условиях
            from src.handlers.chat.datalog import log_call
            log_call(agent, input, result, int((time.monotonic() - started) * 1000), err)
        except Exception:
            pass
