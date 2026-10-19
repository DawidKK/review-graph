"""Read-only terminal review: HEAD against the current working tree."""

import argparse
import asyncio
import difflib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.util import ClassNotFound
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import Footer, Label, OptionList, Static

from review_graph.cli import GraphSession, changes, git, send_selection

LIMIT = 2 * 1024 * 1024


@dataclass
class FileDiff:
    old: list[str]
    new: list[str]
    removed: set[int]
    added: set[int]
    message: str = ""
    rows: list[tuple[int | None, int | None]] = field(default_factory=list)


def load_diff(root: Path, path: str, status: dict) -> FileDiff:
    """Compare a rename's original HEAD blob with its current worktree path."""
    old_path = status.get("old_path", path)
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", f"HEAD:{old_path}"],
        capture_output=True,
        check=False,
    )
    before = b""
    if result.returncode == 0 and not status["untracked"]:
        oid = result.stdout.decode().strip()
        if git(root, "cat-file", "-t", oid).strip() != b"blob":
            return FileDiff([], [], set(), set(), "Submodule — inspect with git diff")
        size = int(git(root, "cat-file", "-s", oid))
        if size > LIMIT:
            return FileDiff(
                [], [], set(), set(), "File exceeds the 2 MiB preview limit"
            )
        before = git(root, "cat-file", "blob", oid)
    target = root / path
    try:
        if target.is_symlink():
            after = os.fsencode(os.readlink(target))
        elif target.is_dir():
            return FileDiff(
                [], [], set(), set(), "Directory / submodule: inspect with git diff"
            )
        elif target.exists():
            with target.open("rb") as stream:
                after = stream.read(LIMIT + 1)
        else:
            after = b""
    except OSError as exc:
        return FileDiff([], [], set(), set(), str(exc))
    if len(after) > LIMIT:
        return FileDiff([], [], set(), set(), "File exceeds the 2 MiB preview limit")
    if b"\0" in before or b"\0" in after:
        return FileDiff([], [], set(), set(), "Binary file — text preview unavailable")
    # Keep line endings for comparison, including a missing final newline.
    old, new = (
        before.decode("utf-8", "replace").splitlines(keepends=True),
        after.decode("utf-8", "replace").splitlines(keepends=True),
    )
    removed, added = set(), set()
    rows = []
    for tag, a, b, c, d in difflib.SequenceMatcher(
        None, old, new, autojunk=True
    ).get_opcodes():
        if tag != "equal":
            removed.update(range(a, b))
            added.update(range(c, d))
        rows.extend(
            (a + i if a + i < b else None, c + i if c + i < d else None)
            for i in range(max(b - a, d - c))
        )
    message = ""
    if before == after:
        message = "Content unchanged (mode, index or rename change)"
    return FileDiff(old, new, removed, added, message, rows)


def display(text: str) -> str:
    """Render paths and source as plain text, never terminal control sequences."""
    return "".join(
        c if c.isprintable() else f"\\x{ord(c):02x}" for c in text.expandtabs(4)
    )


BASE = "#161b22"
REMOVED = "#ff8996"
ADDED = "#7ee2a8"
REMOVED_BACKGROUND = "#542b34"
ADDED_BACKGROUND = "#214b35"


def syntax_lines(lines: list[str], path: str) -> list[Text]:
    """Lex the entire file so multiline strings and comments retain their context."""
    if not lines:
        return []
    code = "\n".join(display(line.rstrip("\r\n")) for line in lines)
    try:
        lexer = get_lexer_for_filename(path, code)
    except ClassNotFound:
        lexer = TextLexer()
    syntax = Syntax(code, lexer, theme="monokai", background_color=BASE)
    return syntax.highlight(code).split("\n", allow_blank=True)


class DiffView(ScrollView):
    """One virtual canvas keeps both versions aligned, including mouse scrolling."""

    DEFAULT_CSS = """
    DiffView { background: #161b22; color: #dce3ed; height: 1fr;
        scrollbar-background: #161b22; scrollbar-color: #45566e; }
    DiffView:focus { border: none; }
    """
    BINDINGS: ClassVar = [
        Binding("j", "scroll_down", show=False),
        Binding("k", "scroll_up", show=False),
        Binding("h", "scroll_left", show=False),
        Binding("l", "scroll_right", show=False),
    ]

    def __init__(self):
        super().__init__(id="diff")
        self.diff = FileDiff([], [], set(), set())
        self.highlighted: tuple[list[Text], list[Text]] = ([], [])
        self.code_width = 0
        self.number_width = 8

    def show_diff(self, diff: FileDiff, highlighted: tuple[list[Text], list[Text]]):
        self.diff = diff
        self.highlighted = highlighted
        self.code_width = max(
            (line.cell_len for side in highlighted for line in side), default=0
        )
        self.number_width = max(4, len(str(max(len(diff.old), len(diff.new))))) + 3
        self.update_size()
        self.scroll_to(0, 0, animate=False, force=True)
        self.refresh()

    def update_size(self):
        # Horizontal scrolling pans the code in each half while gutters stay fixed.
        half = max(1, (self.scrollable_content_region.width - 1) // 2)
        overflow = max(0, self.code_width - max(1, half - self.number_width))
        self.virtual_size = Size(
            self.scrollable_content_region.width + overflow, max(1, len(self.diff.rows))
        )

    def on_resize(self):
        self.update_size()

    def styled_line(self, side: int, row: int, width: int) -> Strip:
        """Highlight the full changed row; keep syntax colors on unchanged lines."""
        index = self.diff.rows[row][side]
        changed = index in (self.diff.removed if side == 0 else self.diff.added)
        foreground = (REMOVED if side == 0 else ADDED) if changed else "#dce3ed"
        background = (
            (REMOVED_BACKGROUND if side == 0 else ADDED_BACKGROUND) if changed else BASE
        )
        style = Style(color=foreground, bgcolor=background)
        marker = ("−" if side == 0 else "+") if changed else " "
        gutter = Text(
            f"{index + 1:>{self.number_width - 3}} {marker} "
            if index is not None
            else " " * self.number_width,
            style=Style(
                color=foreground if changed else "#63758b",
                bgcolor=background,
            ),
        )
        code = self.highlighted[side][index].copy() if index is not None else Text()
        code.stylize(style if changed else Style(bgcolor=BASE))
        code_strip = Strip(
            segment for segment in code.render(self.app.console) if segment.text
        )
        content_width = max(0, width - self.number_width)
        offset = int(self.scroll_x)
        cropped = code_strip.apply_style(style).crop_extend(
            offset, offset + content_width, style
        )
        return Strip.join(
            [
                Strip(gutter.render(self.app.console)).apply_style(gutter.style),
                cropped,
            ]
        ).crop_extend(0, width, style)

    def render_line(self, y: int) -> Strip:
        width = self.scrollable_content_region.width
        row = y + int(self.scroll_y)
        if row >= len(self.diff.rows):
            if not self.diff.rows and y == 0:
                text = Text(self.diff.message or "Empty file", style="#94a5ba")
                return (
                    Strip(text.render(self.app.console))
                    .apply_style(Style(color="#94a5ba", bgcolor=BASE))
                    .crop_extend(0, width, Style(bgcolor=BASE))
                )
            return Strip.blank(width, Style(bgcolor=BASE))
        left = max(0, (width - 1) // 2)
        return Strip.join(
            [
                self.styled_line(0, row, left),
                Strip([Segment("│", Style(color="#344256", bgcolor=BASE))]),
                self.styled_line(1, row, max(0, width - left - 1)),
            ]
        )


class TerminalReview(App):
    TITLE = "Review Graph"
    CSS = """
    Screen { background: #0e141c; color: #dce3ed; }
    #title { height: 3; padding: 1 2; background: #1c2634; color: #87c5ff; text-style: bold; }
    #body { height: 1fr; }
    #sidebar { width: 28; min-width: 18; max-width: 32; border-right: solid #344256; }
    #files-title { height: 3; padding: 1 2; color: #94a5ba; text-style: bold; }
    #files { height: 1fr; padding: 0 1; background: #0e141c; border: none; }
    #files:focus { border: none; }
    #files > .option-list--option-highlighted { background: #273e57; color: #ffffff; }
    #review { width: 1fr; }
    #versions { height: 3; background: #1c2634; }
    #versions Label { width: 1fr; padding: 1 2; text-style: bold; }
    #old-title { color: #ff8996; border-right: solid #344256; }
    #new-title { color: #7ee2a8; }
    #summary { height: 1; padding: 0 1; background: #1c2634; }
    #notice { height: 1; padding: 0 1; color: #94a5ba; }
    Footer { background: #1c2634; }
    """
    BINDINGS: ClassVar = [
        Binding("q,escape", "quit", "Quit"),
        Binding("r", "reload", "Refresh"),
        Binding("g", "graph", "Graph"),
        Binding("[", "change(-1)", "Previous change", key_display="["),
        Binding("]", "change(1)", "Next change", key_display="]"),
        Binding("enter", "focus_code", "Code", show=False),
        Binding("j", "file_move(1)", show=False),
        Binding("k", "file_move(-1)", show=False),
    ]

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.graph_session = GraphSession(root)
        self.files = []
        self.selected = 0
        self.diff = FileDiff([], [], set(), set())
        self.selection_lock = asyncio.Lock()
        self.last_synced = object()
        self.reload()

    def reload(self):
        previous = self.files[self.selected][0] if self.files else None
        self.files = sorted(
            (p, s) for p, s in changes(self.root).items() if "renamed_to" not in s
        )
        self.selected = next(
            (i for i, (p, _) in enumerate(self.files) if p == previous),
            min(self.selected, max(0, len(self.files) - 1)),
        )
        self.diff = (
            load_diff(self.root, *self.files[self.selected])
            if self.files
            else FileDiff([], [], set(), set(), "Working tree clean")
        )

    def compose(self) -> ComposeResult:
        yield Static(
            Text.assemble(("REVIEW GRAPH   ", "bold #87c5ff"), display(str(self.root))),
            id="title",
        )
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Label("CHANGED FILES", id="files-title")
                yield OptionList(id="files")
            with Vertical(id="review"):
                with Horizontal(id="versions"):
                    yield Label("OLD · HEAD", id="old-title")
                    yield Label("NEW · WORKTREE", id="new-title")
                yield DiffView()
        yield Static(id="summary", markup=False)
        yield Static("HEAD → worktree · read only", id="notice", markup=False)
        yield Footer()

    def on_mount(self):
        self.populate_files()
        self.show_diff()
        self.query_one("#files").focus()
        self.graph("start")

    def populate_files(self):
        options = self.query_one("#files", OptionList)
        with options.prevent(OptionList.OptionHighlighted):
            options.clear_options()
            options.add_options(
                Text.assemble((status["xy"] + "  ", "#87c5ff"), display(path))
                for path, status in self.files
            )
            options.highlighted = self.selected if self.files else None
        self.query_one("#files-title", Label).update(
            f"CHANGED FILES  {len(self.files)}"
        )

    def show_diff(self, highlighted=None):
        path = self.files[self.selected][0] if self.files else ""
        if highlighted is None:
            highlighted = (
                syntax_lines(self.diff.old, path),
                syntax_lines(self.diff.new, path),
            )
        self.query_one(DiffView).show_diff(self.diff, highlighted)
        summary = Text(display(path) or "No changed files")
        summary.append(f"   +{len(self.diff.added)}", style="#7ee2a8")
        summary.append(f"  −{len(self.diff.removed)}", style="#ff8996")
        if self.diff.message:
            summary.append("   " + self.diff.message)
        self.query_one("#summary", Static).update(summary)
        self.sync_selection()

    @work(group="selection")
    async def sync_selection(self):
        # Serialize sends and coalesce quick keyboard navigation to the latest file.
        async with self.selection_lock:
            await asyncio.sleep(0.08)
            path = self.files[self.selected][0] if self.files else None
            if path == self.last_synced:
                return
            try:
                if await asyncio.to_thread(send_selection, self.root, path):
                    self.last_synced = path
            except OSError, ValueError, subprocess.CalledProcessError:
                pass  # Reviewing files remains available if the graph is stopped.

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted):
        if event.option_index != self.selected:
            self.selected = event.option_index
            self.load_selected()

    def on_option_list_option_selected(self):
        self.action_focus_code()

    @work(exclusive=True, group="file")
    async def load_selected(self):
        path, status = self.files[self.selected]
        self.query_one("#summary", Static).update(f"Loading {display(path)}…")
        try:
            diff = await asyncio.to_thread(load_diff, self.root, path, status)
            highlighted = await asyncio.to_thread(
                lambda: (syntax_lines(diff.old, path), syntax_lines(diff.new, path))
            )
            self.diff = diff
            self.show_diff(highlighted)
        except (OSError, subprocess.CalledProcessError) as exc:
            self.query_one("#notice", Static).update(str(exc))

    @work(exclusive=True, group="graph")
    async def graph(self, action):
        self.query_one("#notice", Static).update("Queuing dependency graph…")
        try:
            requested = await asyncio.to_thread(self.graph_session.request, action)
            notice = (
                "Graph requested · analysis runs in background"
                if requested
                else "Graph session closed"
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            notice = str(exc)
        self.query_one("#notice", Static).update(notice)
        self.last_synced = object()
        self.sync_selection()

    async def action_reload(self):
        self.workers.cancel_group(self, "file")
        try:
            await asyncio.to_thread(self.reload)
            self.populate_files()
            self.show_diff()
            self.graph("refresh")
        except (OSError, subprocess.CalledProcessError) as exc:
            self.query_one("#notice", Static).update(str(exc))

    def action_graph(self):
        self.graph("open")

    def action_focus_code(self):
        self.query_one(DiffView).focus()

    def action_file_move(self, delta):
        options = self.query_one("#files", OptionList)
        if delta > 0:
            options.action_cursor_down()
        else:
            options.action_cursor_up()

    def action_change(self, direction):
        view = self.query_one(DiffView)
        changed = [
            i
            for i, (old, new) in enumerate(self.diff.rows)
            if old in self.diff.removed or new in self.diff.added
        ]
        starts = [n for i, n in enumerate(changed) if i == 0 or n != changed[i - 1] + 1]
        targets = [
            n
            for n in starts
            if (n > view.scroll_y if direction > 0 else n < view.scroll_y)
        ]
        if targets:
            view.scroll_to(
                y=targets[0] if direction > 0 else targets[-1], animate=False
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".")
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("review needs an interactive terminal")
    try:
        root = Path(
            os.fsdecode(
                git(Path(args.path).resolve(), "rev-parse", "--show-toplevel")
            ).strip()
        )
        review = TerminalReview(root)
        try:
            review.run()
        finally:
            review.graph_session.close()
    except KeyboardInterrupt:
        pass
    except (OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"review: {exc}\n")


if __name__ == "__main__":
    main()
