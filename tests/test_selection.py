"""Exercise the authenticated selection channel without Graphify or browser launches."""

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from review_graph.cli import Review, create_server, send_selection, state_path


class SelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        state = state_path(self.root)
        state.mkdir()
        with patch.object(Review, "worker"):
            self.server, self.review = create_server(self.root, state)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)

    def selection(self):
        with urllib.request.urlopen(
            self.review.url + "selection", timeout=2
        ) as response:
            return json.load(response)

    def test_selection_retained_for_late_page_and_latest_file_wins(self):
        path = 'src/żółć "\n?.py'
        with patch.object(self.review, "request") as request:
            self.assertTrue(send_selection(self.root, path))
            self.assertEqual(self.selection(), {"revision": 1, "path": path})
            self.assertTrue(send_selection(self.root, path))
            self.assertEqual(self.selection()["revision"], 1)
            self.assertTrue(send_selection(self.root, "new.py"))
            self.assertEqual(self.selection(), {"revision": 2, "path": "new.py"})
            self.assertTrue(send_selection(self.root, None))
            self.assertEqual(self.selection(), {"revision": 3, "path": None})
            request.assert_not_called()
        self.assertFalse(self.review.pending)
        self.assertFalse(self.review.opened)

    def test_mutation_requires_token_and_rejects_invalid_paths(self):
        def post(path, token):
            return urllib.request.urlopen(
                urllib.request.Request(
                    self.review.url + "selection",
                    data=json.dumps({"path": path}).encode(),
                    headers={"X-Review-Token": token},
                ),
                timeout=2,
            )

        with self.assertRaises(urllib.error.HTTPError) as error:
            post("valid.py", "wrong")
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        for path in ("../outside", "/absolute", "a/../b", 42, "", "bad\0path"):
            with (
                self.subTest(path=path),
                self.assertRaises(urllib.error.HTTPError) as error,
            ):
                post(path, self.review.token)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        self.assertIsNone(self.selection()["path"])

    def test_missing_server_does_not_start_one(self):
        (state_path(self.root) / "server.json").unlink()
        self.assertFalse(send_selection(self.root, "file.py"))
