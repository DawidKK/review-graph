"""Exercise real Git porcelain and Graphify-to-file projection, independent of user files."""

import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from review_graph.cli import Review, aggregate, changes


class GraphTests(unittest.TestCase):
    def test_directed_one_hop_with_evidence_and_missing_deleted_file(self):
        root = Path("/tmp").resolve()
        graph = {
            "nodes": [{"id": name, "source_file": f"{name}.py"} for name in "abcd"]
            + [{"id": "external"}, {"id": "a2", "source_file": "a.py"}],
            "edges": [
                {"source": "a", "target": "b", "relation": "imports", "confidence": "EXTRACTED"},
                {"source": "b", "target": "a2", "relation": "calls"},
                {"source": "b", "target": "c", "relation": "calls"},
                {"source": "a", "target": "a2", "relation": "contains"},
                {"source": "a", "target": "external", "relation": "imports"},
            ],
        }
        changed = {"a.py": {"staged": True}, "gone.py": {"deleted": True}}
        data = aggregate(graph, changed, root)
        self.assertEqual({n["id"] for n in data["nodes"]}, {"a.py", "b.py", "gone.py"})
        self.assertEqual(
            [(e["source"], e["target"]) for e in data["edges"]],
            [("a.py", "b.py"), ("b.py", "a.py")],
        )
        self.assertEqual(data["edges"][0]["evidence"], [graph["edges"][0]])
        self.assertFalse(next(n for n in data["nodes"] if n["id"] == "gone.py")["in_graph"])
        graph["links"] = graph.pop("edges")
        self.assertEqual(aggregate(graph, changed, root), data)

    def test_no_changes_means_empty_graph(self):
        self.assertEqual(aggregate({"nodes": [], "edges": []}, {}, Path("/tmp"))["nodes"], [])

    def test_git_index_worktree_untracked_deleted_rename_and_special_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(["git", "-C", directory, *args])

            git("init", "-q")
            for name in ("both.py", "deleted.py", "old name.py"):
                (root / name).write_text("first\n")
            git("add", ".")
            git(
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            )
            (root / "both.py").write_text("staged\n")
            git("add", "both.py")
            (root / "both.py").write_text("unstaged\n")
            (root / "deleted.py").unlink()
            git("mv", "old name.py", "new name.py")
            special = 'żółć "\n?.py'
            (root / special).write_text("untracked\n")
            actual = changes(root)
            self.assertEqual(actual["both.py"]["xy"], "MM")
            self.assertTrue(actual["both.py"]["staged"])
            self.assertTrue(actual["both.py"]["unstaged"])
            self.assertTrue(actual["deleted.py"]["deleted"])
            self.assertTrue(actual[special]["untracked"])
            self.assertEqual(actual["new name.py"]["old_path"], "old name.py")
            self.assertTrue(actual["old name.py"]["deleted"])

    def test_refreshes_never_reopen_browser_or_take_focus(self):
        review = Review.__new__(Review)
        review.condition = threading.Condition()
        review.opened, review.open_after, review.pending = True, False, False
        for _ in range(5):
            review.request("refresh")
        self.assertFalse(review.open_after)
        self.assertTrue(review.pending)
        review.last_seen, review.url = 0, "http://127.0.0.1:1234/token/"
        with patch("review_graph.cli.subprocess.run") as run:
            review.open_browser()
            review.open_browser()
            run.assert_called_once_with(["open", "-g", review.url], check=True)

    @unittest.skipUnless(shutil.which("graphify"), "Graphify not installed")
    def test_real_graphify_worker_refreshes_and_keeps_previous_data_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "base.py").write_text("def helper():\n    return 1\n")
            (root / "caller.py").write_text("from base import helper\n\nvalue = helper()\n")
            (root / "gone.py").write_text("x = 1\n")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            (root / "caller.py").write_text("from base import helper\n\nvalue = helper() + 1\n")
            (root / "gone.py").unlink()
            (root / "new.py").write_text("x = 2\n")
            state = root / ".git/review-graph"
            state.mkdir()
            review = Review(root, state, "test")
            review.opened = True

            def wait_for_result():
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    with review.condition:
                        if not review.pending and not review.busy:
                            return
                    time.sleep(0.05)
                self.fail("Graphify worker did not finish")

            with patch.object(review, "open_browser") as browser:
                review.request("refresh")
                wait_for_result()
                self.assertIsNone(review.error)
                nodes = {n["id"]: n for n in review.data["nodes"]}
                self.assertFalse(nodes["base.py"]["changed"])
                self.assertTrue(nodes["caller.py"]["changed"])
                self.assertTrue(nodes["gone.py"]["status"]["deleted"])
                self.assertTrue(nodes["new.py"]["status"]["untracked"])
                self.assertTrue(
                    any(
                        e["source"] == "caller.py" and e["target"] == "base.py"
                        for e in review.data["edges"]
                    )
                )
                first = review.data
                review.request("refresh")
                wait_for_result()
                self.assertEqual(review.revision, 2)
                with patch("review_graph.cli.shutil.which", return_value=None):
                    review.request("refresh")
                    wait_for_result()
                self.assertIsNotNone(review.error)
                self.assertEqual(review.data["nodes"], first["nodes"])
                self.assertEqual(review.revision, 2)
                browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
