"""Child processes that cannot outlive us (port of ChildProcessJob.cs).

Windows had a Job Object: every ffmpeg went in it, and the OS killed them all when the server died -
including the paths that never run our own cleanup (killed in a task manager, hard crash). Otherwise an
orphaned ffmpeg kept holding the screen capture and blocked the next launch.

Linux has no job objects. The equivalent is three layers, cheapest first:
 - PR_SET_PDEATHSIG=SIGKILL in the child (prctl via ctypes in preexec_fn): the kernel kills the child
   the moment its parent is gone, whatever the reason - no cleanup code of ours needs to run.
 - a registry of everything we spawned, so kill_all() can reap on the orderly paths (shutdown, atexit).
 - stdin=DEVNULL by default: a child inheriting the terminal would read the user's keystrokes.

The pdeathsig trap: the signal fires when the THREAD that forked the child exits, not when the process
does (prctl(2)). A pump thread that spawns ffmpeg and then ends while the child should live on would kill
it. So every child is forked from one long-lived spawner thread, which only ends with the process - that
gives the process-lifetime semantics the Job Object had.
"""

import atexit
import ctypes
import os
import queue
import signal
import subprocess
import sys
import threading

from . import log
from .i18n import _

PR_SET_PDEATHSIG = 1
SPAWN_TIMEOUT_S = 10.0       # a fork never takes this long; only hit if the spawner is wedged during shutdown
REAP_TIMEOUT_S = 1.0

_libc = ctypes.CDLL(None, use_errno=True)
_prctl = _libc.prctl
_prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
_prctl.restype = ctypes.c_int

_OUR_PID = os.getpid()
_gate = threading.Lock()
_children: list[subprocess.Popen] = []


def _die_with_parent() -> None:
    """Runs in the child between fork and exec. Keep it tiny: only async-signal-safe-ish work here."""
    _prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    # the parent may have died in the gap between fork and prctl - the signal would then never come.
    # getppid still reporting us means it is alive; anything else (init, a subreaper) means it is not.
    if os.getppid() != _OUR_PID:
        os._exit(1)


class _Request:
    """One spawn handed to the spawner thread. `abandoned` closes the race that would otherwise leak a
    child: the caller gives up after SPAWN_TIMEOUT_S and starts its own, and the spawner's Popen then
    lands in a box nobody reads - an ffmpeg holding the screen capture that nothing ever kills."""

    __slots__ = ("args", "kw", "process", "error", "done", "gate", "abandoned")

    def __init__(self, args, kw):
        self.args, self.kw = args, kw
        self.process: subprocess.Popen | None = None
        self.error: BaseException | None = None
        self.done = threading.Event()
        self.gate = threading.Lock()
        self.abandoned = False


class _Spawner:
    """One thread that forks all children, so pdeathsig binds to the process lifetime (see module doc)."""

    def __init__(self):
        self._requests: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_gate = threading.Lock()

    def _ensure_thread(self) -> None:
        with self._start_gate:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="childproc-spawn", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            request = self._requests.get()
            process = error = None
            try:
                process = subprocess.Popen(request.args, **request.kw)
            except BaseException as caught:   # noqa: BLE001 - handed back to the caller, whatever it is
                error = caught
            with request.gate:
                if request.abandoned and process is not None:
                    _discard(process)        # nobody is waiting for it any more
                else:
                    request.process, request.error = process, error
            request.done.set()

    def spawn(self, args, kw) -> subprocess.Popen:
        if sys.is_finalizing() or threading.current_thread() is self._thread:
            return subprocess.Popen(args, **kw)   # no other thread will serve us any more
        self._ensure_thread()
        request = _Request(args, kw)
        self._requests.put(request)
        if not request.done.wait(SPAWN_TIMEOUT_S):
            with request.gate:   # hand the spawner's result back if it arrived while we were giving up
                request.abandoned = request.process is None and request.error is None
                give_up = request.abandoned
            if give_up:
                log.write(_("childproc: the spawner does not answer, starting directly"))
                return subprocess.Popen(args, **kw)
        if request.error is not None:
            raise request.error
        return request.process


def _discard(process: subprocess.Popen) -> None:
    """Kill and reap a child the caller never got to see."""
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=REAP_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        pass


_spawner = _Spawner()


def popen(args, **kw) -> subprocess.Popen:
    """subprocess.Popen, but the child dies with us and is remembered for kill_all()."""
    kw.setdefault("stdin", subprocess.DEVNULL)
    caller_preexec = kw.get("preexec_fn")
    if caller_preexec is None:
        kw["preexec_fn"] = _die_with_parent
    else:
        def chained():
            _die_with_parent()
            caller_preexec()
        kw["preexec_fn"] = chained

    process = _spawner.spawn(args, kw)
    with _gate:
        _children[:] = [child for child in _children if child.poll() is None]   # forget the ones already gone
        _children.append(process)
    return process


def children() -> list[subprocess.Popen]:
    """Snapshot of the registered children that have not been reaped yet (mostly for tests)."""
    with _gate:
        return [child for child in _children if child.poll() is None]


def kill_all() -> None:
    """The orderly-path reaper: SIGKILL everything still running, then wait so nothing is left a zombie."""
    with _gate:
        running = [child for child in _children if child.poll() is None]
        _children.clear()
    for child in running:
        try:
            child.kill()
        except OSError:
            pass
    for child in running:
        try:
            child.wait(timeout=REAP_TIMEOUT_S)
        except (subprocess.TimeoutExpired, OSError):
            pass


atexit.register(kill_all)
