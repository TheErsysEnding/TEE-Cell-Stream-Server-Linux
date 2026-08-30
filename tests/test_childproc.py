"""childproc.py: children die with us (PR_SET_PDEATHSIG), survive the thread that spawned them, and are reaped.

Run: cd <project> && PYTHONPATH=src python3 -m unittest tests.test_childproc -v
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

_SCRATCH = tempfile.mkdtemp(prefix="tee-cst-test-childproc-")
os.environ.setdefault("TEE_CST_SETTINGS_PATH", os.path.join(_SCRATCH, "settings.json"))
os.environ.setdefault("TEE_CST_LOG_PATH", os.path.join(_SCRATCH, "server.log"))
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from teecellstream import childproc, log   # noqa: E402


def _pids_of(argv):
    """pids running exactly this argv. Exact, not a substring search: a shell whose command line happens
    to quote the same words would otherwise be counted as one of them."""
    wanted = [part.encode() for part in argv]
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as handle:
                parts = handle.read().split(b"\0")
        except OSError:
            continue
        while parts and parts[-1] == b"":
            parts.pop()
        if parts == wanted:
            found.append(int(entry))
    return sorted(found)


def _process_state(pid):
    """None when the pid is gone, otherwise the state letter from /proc (Z = zombie)."""
    try:
        with open("/proc/%d/stat" % pid) as handle:
            return handle.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def _wait_gone(pid, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_state(pid) in (None, "Z"):
            return True
        time.sleep(0.02)
    return _process_state(pid) in (None, "Z")


class ChildProcTests(unittest.TestCase):
    def test_stdin_defaults_to_devnull(self):
        # a child inheriting the terminal would eat the user's keystrokes; cat on /dev/null ends at once
        process = childproc.popen(["cat"], stdout=subprocess.PIPE)
        out, _err = process.communicate(timeout=5)
        self.assertEqual(out, b"")
        self.assertEqual(process.returncode, 0)

    def test_registry_and_kill_all(self):
        process = childproc.popen(["sleep", "30"])
        self.assertIn(process, childproc.children())
        childproc.kill_all()
        self.assertEqual(process.returncode, -9)
        self.assertNotIn(process, childproc.children())
        self.assertIsNone(_process_state(process.pid), "nicht abgeräumt (Zombie?)")

    def test_exited_children_leave_the_registry(self):
        process = childproc.popen(["true"])
        process.wait(timeout=5)
        childproc.popen(["true"]).wait(timeout=5)   # the next popen purges the finished ones
        self.assertNotIn(process, childproc.children())

    def test_child_survives_the_thread_that_spawned_it(self):
        # pdeathsig binds to the forking THREAD; the spawner thread makes it bind to the process instead
        box = {}
        thread = threading.Thread(target=lambda: box.__setitem__("process", childproc.popen(["sleep", "30"])))
        thread.start()
        thread.join()
        time.sleep(0.3)
        process = box["process"]
        self.assertIsNone(process.poll(), "das Kind starb mit dem Thread, der es startete")
        process.kill()
        process.wait(timeout=5)

    def test_child_dies_with_its_parent(self):
        # a middle python uses childproc to start a grandchild, then gets SIGKILLed (no cleanup runs):
        # the kernel must take the grandchild down with it
        script = ("import os, sys, time\n"
                  "sys.path.insert(0, %r)\n"
                  "from teecellstream import childproc\n"
                  "child = childproc.popen(['sleep', '30'])\n"
                  "print(child.pid, flush=True)\n"
                  "time.sleep(60)\n") % _SRC
        env = dict(os.environ)
        env["TEE_CST_LOG_PATH"] = os.path.join(_SCRATCH, "middle.log")
        env["TEE_CST_SETTINGS_PATH"] = os.path.join(_SCRATCH, "middle.json")
        middle = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, env=env)
        try:
            grandchild_pid = int(middle.stdout.readline().strip())
            self.assertNotIn(_process_state(grandchild_pid), (None, "Z"))
            middle.kill()
            middle.wait(timeout=5)
            self.assertTrue(_wait_gone(grandchild_pid, 3.0),
                            "Enkel (pid %d) überlebte den Tod des Vaters: %s" % (grandchild_pid, _process_state(grandchild_pid)))
        finally:
            if middle.poll() is None:
                middle.kill()
                middle.wait(timeout=5)
            middle.stdout.close()

    def test_callers_preexec_fn_still_runs(self):
        marker = os.path.join(_SCRATCH, "preexec-ran")
        process = childproc.popen(["true"], preexec_fn=lambda: open(marker, "w").close())
        process.wait(timeout=5)
        self.assertTrue(os.path.exists(marker))

    def test_a_spawn_the_caller_gave_up_on_is_not_left_running(self):
        """The caller waits SPAWN_TIMEOUT_S and then forks the child itself. Whatever the wedged spawner
        thread produces afterwards belongs to nobody: it is in no registry, so kill_all() would never see
        it, and an ffmpeg like that keeps the screen capture until the server exits. It must be reaped."""
        real_popen = subprocess.Popen

        def slow_popen(args, **kw):
            time.sleep(1.0)
            return real_popen(args, **kw)

        shim = types.SimpleNamespace(Popen=slow_popen, DEVNULL=subprocess.DEVNULL,
                                     TimeoutExpired=subprocess.TimeoutExpired)
        saved_module, saved_timeout = childproc.subprocess, childproc.SPAWN_TIMEOUT_S
        childproc.subprocess, childproc.SPAWN_TIMEOUT_S = shim, 0.2
        try:
            process = childproc.popen(["sleep", "37.3"])
        finally:
            childproc.subprocess, childproc.SPAWN_TIMEOUT_S = saved_module, saved_timeout
        self.addCleanup(lambda: (process.poll() is None and process.kill(), process.wait(timeout=5)))

        self.assertIn(process, childproc.children())
        self.assertIn("childproc: the spawner does not answer, starting directly", log.get_recent())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            pids = _pids_of(["sleep", "37.3"])
            if pids == [process.pid]:
                break
            time.sleep(0.05)
        self.assertEqual(_pids_of(["sleep", "37.3"]), [process.pid],
                         "der aufgegebene Start blieb als Waise übrig: %r" % _pids_of(["sleep", "37.3"]))

    def test_grandchild_from_a_short_lived_thread_still_dies_with_its_parent(self):
        """Both halves at once: the child is forked from a thread that ends right away (pdeathsig would
        fire immediately without the spawner thread), and the middle process is then SIGKILLed (only
        pdeathsig can reap the grandchild)."""
        script = ("import os, sys, threading, time\n"
                  "sys.path.insert(0, %r)\n"
                  "from teecellstream import childproc\n"
                  "box = {}\n"
                  "thread = threading.Thread(target=lambda: box.__setitem__('c', childproc.popen(['sleep', '40'])))\n"
                  "thread.start(); thread.join()\n"
                  "time.sleep(0.5)\n"
                  "assert box['c'].poll() is None, 'starb mit dem Thread'\n"
                  "print(box['c'].pid, flush=True)\n"
                  "time.sleep(60)\n") % _SRC
        env = dict(os.environ)
        env["TEE_CST_LOG_PATH"] = os.path.join(_SCRATCH, "middle2.log")
        env["TEE_CST_SETTINGS_PATH"] = os.path.join(_SCRATCH, "middle2.json")
        middle = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, env=env)
        try:
            line = middle.stdout.readline().strip()
            self.assertTrue(line, "das Kind überlebte den Thread nicht, der es startete")
            grandchild_pid = int(line)
            middle.kill()
            middle.wait(timeout=5)
            self.assertTrue(_wait_gone(grandchild_pid, 3.0),
                            "Enkel (pid %d) überlebte: %s" % (grandchild_pid, _process_state(grandchild_pid)))
        finally:
            if middle.poll() is None:
                middle.kill()
                middle.wait(timeout=5)
            middle.stdout.close()

    def test_failed_spawn_raises_and_is_not_registered(self):
        with self.assertRaises(OSError):
            childproc.popen(["/nonexistent/tee-cst-no-such-binary"])
        self.assertTrue(all(child.args[0] != "/nonexistent/tee-cst-no-such-binary" for child in childproc.children()))


if __name__ == "__main__":
    unittest.main()
