"""Review actual HEAD/worktree snapshots without modifying the user's repository."""

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_graph.cli import changes
from review_graph.terminal import (
    ADDED,
    ADDED_BACKGROUND,
    BASE,
    REMOVED,
    REMOVED_BACKGROUND,
    DiffView,
    TerminalReview,
    display,
    load_diff,
)


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "-q")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args])

    def commit(self):
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )

    def diff(self, name):
        return load_diff(self.root, name, changes(self.root)[name])

    def test_head_against_worktree_includes_staged_and_unstaged(self):
        file = self.root / "file.txt"
        file.write_text("same\nold\n")
        self.commit()
        file.write_text("same\nstaged\n")
        self.git("add", ".")
        file.write_text("same\ncurrent\nextra\n")
        diff = self.diff("file.txt")
        self.assertEqual(diff.old, ["same\n", "old\n"])
        self.assertEqual(diff.new, ["same\n", "current\n", "extra\n"])
        self.assertEqual(diff.removed, {1})
        self.assertEqual(diff.added, {1, 2})
        self.assertEqual(changes(self.root)["file.txt"]["xy"], "MM")

    def test_rename_delete_and_new_file(self):
        (self.root / "old name").write_text("original\n")
        (self.root / "gone").write_text("deleted\n")
        self.commit()
        self.git("mv", "old name", "new name")
        (self.root / "new name").write_text("original\nadded\n")
        (self.root / "gone").unlink()
        (self.root / "żółć\n.txt").write_text("new\n")
        diff = self.diff("new name")
        self.assertEqual(diff.old, ["original\n"])
        self.assertEqual(diff.added, {1})
        self.assertEqual(self.diff("gone").removed, {0})
        self.assertEqual(self.diff("żółć\n.txt").added, {0})
        review = TerminalReview(self.root)
        self.assertNotIn("old name", [p for p, _ in review.files])

    def test_unborn_repository_binary_and_symlink(self):
        (self.root / "new").write_text("new")
        self.assertEqual(self.diff("new").added, {0})
        (self.root / "binary").write_bytes(b"a\0b")
        self.assertIn("Binary", self.diff("binary").message)
        (self.root / "link").symlink_to("/etc/passwd")
        self.assertEqual(self.diff("link").new, ["/etc/passwd"])

    def test_final_newline_and_clean_refresh(self):
        (self.root / "file").write_text("line\n")
        self.commit()
        (self.root / "file").write_text("line")
        self.assertEqual(self.diff("file").added, {0})
        review = TerminalReview(self.root)
        (self.root / "file").write_text("line\n")
        review.reload()
        self.assertEqual(review.files, [])
        self.assertEqual(review.diff.message, "Working tree clean")

    def test_side_by_side_alignment_keeps_context_after_unequal_changes(self):
        file = self.root / "file"
        file.write_text("start\nremoved\nkeep\nold\nend\n")
        self.commit()
        file.write_text("inserted\nstart\nkeep\nnew one\nnew two\nend\n")
        diff = self.diff("file")
        pairs = [
            (
                diff.old[a].strip() if a is not None else None,
                diff.new[b].strip() if b is not None else None,
            )
            for a, b in diff.rows
        ]
        self.assertEqual(
            pairs,
            [
                (None, "inserted"),
                ("start", "start"),
                ("removed", None),
                ("keep", "keep"),
                ("old", "new one"),
                (None, "new two"),
                ("end", "end"),
            ],
        )

    def test_textual_diff_text_and_full_row_background_colors_and_scroll(self):
        file = self.root / "example.py"
        tail = "# " + "long comment " * 20 + "\n"
        file.write_text(
            'value = "before"\n' + 'context = "value" # context\n' * 60 + tail
        )
        self.commit()
        file.write_text(
            'value = "after"\nextra = 123\n'
            + 'context = "value" # context\n' * 60
            + tail
        )

        async def check():
            app = TerminalReview(self.root)
            with patch.object(app, "graph"):
                async with app.run_test(size=(140, 30)) as pilot:
                    view = app.query_one(DiffView)
                    old = view.styled_line(0, 0, 50)
                    new = view.styled_line(1, 0, 50)
                    self.assertIn('"before"', old.text)
                    self.assertIn('"after"', new.text)
                    for strip, foreground, background in (
                        (old, REMOVED, REMOVED_BACKGROUND),
                        (new, ADDED, ADDED_BACKGROUND),
                    ):
                        self.assertEqual(strip.cell_length, 50)
                        for segment in strip:
                            self.assertEqual(
                                segment.style.bgcolor.get_truecolor().hex, background
                            )
                            if segment.text.strip():
                                self.assertEqual(
                                    segment.style.color.get_truecolor().hex, foreground
                                )
                    unchanged = view.styled_line(0, 2, 70)
                    syntax_colors = set()
                    for segment in unchanged:
                        self.assertEqual(
                            segment.style.bgcolor.get_truecolor().hex, BASE
                        )
                        if (
                            '"' in segment.text
                            or "context" in segment.text
                            or "value" in segment.text
                        ):
                            syntax_colors.add(segment.style.color)
                    self.assertGreater(
                        len(syntax_colors),
                        1,
                        "Unchanged lines must retain syntax highlighting",
                    )
                    self.assertEqual(view.styled_line(0, 1, 50).text.strip(), "")
                    self.assertIn("extra", view.styled_line(1, 1, 50).text)
                    await pilot.press("enter", "down", "down", "down")
                    await pilot.pause()
                    self.assertGreater(view.scroll_y, 0)
                    strip = view.render_line(0)
                    self.assertEqual(strip.text.count("# context"), 2)
                    await pilot.resize_terminal(90, 20)
                    await pilot.pause()
                    self.assertEqual(
                        view.render_line(0).cell_length,
                        view.scrollable_content_region.width,
                    )
                    gutter = view.styled_line(0, 2, 40).text[: view.number_width]
                    await pilot.press("right", "right")
                    await pilot.pause()
                    self.assertGreater(view.scroll_x, 0)
                    self.assertEqual(
                        view.styled_line(0, 2, 40).text[: view.number_width], gutter
                    )
                    self.assertNotIn("# context", view.render_line(0).text)

        asyncio.run(check())

    def test_textual_file_selection_and_refresh(self):
        (self.root / "a.py").write_text("x = 1\n")
        (self.root / "b.txt").write_text("original\n")
        self.commit()
        (self.root / "a.py").write_text("x = 2\n")
        (self.root / "b.txt").write_text("changed\n")

        async def check():
            app = TerminalReview(self.root)
            with (
                patch.object(app, "graph"),
                patch(
                    "review_graph.terminal.send_selection", return_value=True
                ) as sync,
            ):
                async with app.run_test(size=(120, 24)) as pilot:
                    await app.workers.wait_for_complete()
                    self.assertEqual(sync.call_args.args, (self.root, "a.py"))
                    await pilot.press("down")
                    await app.workers.wait_for_complete()
                    self.assertEqual(app.files[app.selected][0], "b.txt")
                    self.assertEqual(sync.call_args.args, (self.root, "b.txt"))
                    self.assertIn(
                        "changed", app.query_one(DiffView).render_line(0).text
                    )
                    (self.root / "b.txt").write_text("refreshed\n")
                    await pilot.press("r")
                    await pilot.pause()
                    self.assertIn(
                        "refreshed", app.query_one(DiffView).render_line(0).text
                    )
                    (self.root / "a.py").write_text("x = 1\n")
                    (self.root / "b.txt").write_text("original\n")
                    await pilot.press("r")
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    self.assertEqual(app.files, [])
                    self.assertEqual(sync.call_args.args, (self.root, None))
                    self.assertIn(
                        "Working tree clean",
                        app.query_one(DiffView).render_line(0).text,
                    )

        asyncio.run(check())

    def test_terminal_control_characters_are_visible_text(self):
        self.assertEqual(display("a\x1b[31m\nb"), "a\\x1b[31m\\x0ab")


if __name__ == "__main__":
    unittest.main()
