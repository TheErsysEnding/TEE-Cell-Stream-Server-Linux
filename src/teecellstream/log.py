"""The server's running commentary: a log file plus the last lines in memory for the window (port of Log.cs)."""

import os
import threading
from collections import deque
from datetime import datetime

RECENT_LINES = 200
MAX_BYTES = 2 * 1024 * 1024

_STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "tee-cell-stream-server")
# TEE_CST_LOG_PATH lets tests keep their noise out of the user's real log
LOG_PATH = os.environ.get("TEE_CST_LOG_PATH") or os.path.join(_STATE_DIR, "server.log")
_STATE_DIR = os.path.dirname(LOG_PATH)

_gate = threading.Lock()
_recent: deque[str] = deque(maxlen=RECENT_LINES)
_generation = 0   # bumps on every write, so the window can skip re-rendering an unchanged log


def write(message: str) -> None:
    global _generation
    line = "[" + datetime.now().strftime("%H:%M:%S.%f")[:-3] + "] " + message
    with _gate:
        _recent.append(line)
        _generation += 1
        try:
            os.makedirs(_STATE_DIR, exist_ok=True)
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_BYTES:
                os.remove(LOG_PATH)
            with open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass   # a log we cannot write must never take the server down


def get_recent() -> str:
    with _gate:
        return "\n".join(_recent) + ("\n" if _recent else "")


def generation() -> int:
    with _gate:
        return _generation
