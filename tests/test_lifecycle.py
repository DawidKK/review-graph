"""Exercise terminal-owned servers with real HTTP and subprocesses, without Graphify."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from review_graph.cli import GraphSession, connect, start_server, state_path
from review_graph.terminal import main


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.state = state_path(self.root)
        self.state.mkdir()
        # Keep extraction in progress so tests never open a browser. Closing the
        # session must also terminate this child, rather than waiting for analysis.
        tools = self.root / "tools"
        tools.mkdir()
        executable = tools / "graphify"
        self.ready = self.root / "extract-ready"
        self.stopped = self.root / "extract-stopped"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import signal, sys, time\nfrom pathlib import Path\n"
            "def stop(*args):\n"
            f"    Path({str(self.stopped)!r}).touch()\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            f"Path({str(self.ready)!r}).touch()\n"
            "time.sleep(60)\n"
        )
        executable.chmod(0o755)
        environment = patch.dict(os.environ, {"PATH": f"{tools}:{os.environ['PATH']}"})
        environment.start()
        self.addCleanup(environment.stop)

    def session(self):
        session = GraphSession(self.root)
        self.addCleanup(session.close)
        return session

    def test_fresh_server_replaces_old_server_and_exit_stops_it(self):
        process, old = start_server(self.root, self.state)
        self.addCleanup(process.wait, 5)
        session = self.session()
        session.request("start")
        self.assertNotEqual(old["pid"], session.info["pid"])
        self.assertNotEqual(old["url"], session.info["url"])
        process.wait(timeout=5)
        self.assertIsNotNone(connect(self.state))
        session.close()
        self.assertIsNone(connect(self.state))
        self.assertIsNotNone(session.process.poll())

    def test_older_session_exit_cannot_stop_newer_session(self):
        older, newer = self.session(), self.session()
        older.request("start")
        newer.request("start")
        older.close()
        self.assertEqual(connect(self.state)["pid"], newer.process.pid)
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(older.info["url"] + "health", timeout=1)
        newer.close()
        self.assertIsNone(connect(self.state))

    def test_request_after_close_does_not_start_server(self):
        session = self.session()
        session.close()
        thread = threading.Thread(target=session.request, args=("start",))
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(session.process)
        self.assertIsNone(connect(self.state))

    def test_exit_during_startup_cleans_up_late_server(self):
        session = self.session()
        started, release = threading.Event(), threading.Event()
        errors = []

        def delayed_start(*args):
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("Test did not release server startup")
            return start_server(*args)

        def request():
            try:
                session.request("start")
            except (OSError, RuntimeError) as exc:
                errors.append(exc)

        with patch("review_graph.cli.start_server", side_effect=delayed_start):
            thread = threading.Thread(target=request)
            thread.start()
            try:
                self.assertTrue(started.wait(timeout=5))
                closer = threading.Thread(target=session.close)
                closer.start()
            finally:
                release.set()
                thread.join(timeout=10)
            closer.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(connect(self.state))

    def test_exit_stops_running_extraction_child(self):
        session = self.session()
        session.request("start")
        deadline = time.monotonic() + 5
        while not self.ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.ready.exists(), "Extraction did not start")
        session.close()
        deadline = time.monotonic() + 5
        while not self.stopped.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.stopped.exists(), "Extraction did not receive SIGTERM")

    def test_refresh_reuses_session_and_does_not_reset_selection(self):
        session = self.session()
        session.request("start")
        info = session.info
        request = urllib.request.Request(
            info["url"] + "selection",
            data=json.dumps({"path": "example.py"}).encode(),
            headers={"X-Review-Token": info["token"]},
        )
        with urllib.request.urlopen(request, timeout=2):
            pass
        session.request("refresh")
        self.assertEqual(connect(self.state)["pid"], info["pid"])
        with urllib.request.urlopen(info["url"] + "selection", timeout=2) as response:
            self.assertEqual(json.load(response)["path"], "example.py")

    def test_terminal_exit_and_exception_both_close_session(self):
        for error in (None, KeyboardInterrupt(), RuntimeError("UI failed")):
            with (
                self.subTest(error=error),
                patch("sys.argv", ["review", str(self.root)]),
                patch("sys.stdin.isatty", return_value=True),
                patch("sys.stdout.isatty", return_value=True),
                patch("review_graph.terminal.TerminalReview") as app,
            ):
                app.return_value.run.side_effect = error
                if isinstance(error, RuntimeError):
                    with self.assertRaises(RuntimeError):
                        main()
                else:
                    main()
                app.return_value.graph_session.close.assert_called_once()
