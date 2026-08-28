"""One shared clock for the whole server (port of StreamSender.NowUs).

The PS3 syncs to it (TIME command) so it can measure how long each frame took from encoder exit to
appearing on screen. Anchored to wall-clock time (microseconds since 2020-01-01 UTC) so a server restart
does not rewind it, but ticking on the monotonic clock so it never jumps.
"""

import time
from datetime import datetime, timezone

_EPOCH_2020 = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
_START_US = int((time.time() - _EPOCH_2020) * 1_000_000)
_START_MONOTONIC_NS = time.monotonic_ns()


def now_us() -> int:
    """Microseconds since 2020-01-01 UTC, monotonic within this process."""
    return _START_US + (time.monotonic_ns() - _START_MONOTONIC_NS) // 1000


def sleep_until_us(due_us: int, spin_margin_us: int = 150) -> None:
    """Waits until the shared clock reaches due_us: sleeps most of the way, spins only the last margin.

    Python cannot spin for long without starving other threads (GIL), so the spin is kept to a hair.
    """
    while True:
        remaining = due_us - now_us()
        if remaining <= 0:
            return
        if remaining > spin_margin_us:
            time.sleep((remaining - spin_margin_us) / 1_000_000)
        # else: spin - the loop simply re-reads the clock
