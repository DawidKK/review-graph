"""Connect the installed command to an existing nvim-review setup, with backups."""

import json
import shutil
import sys
import time
from pathlib import Path


def install() -> None:
    home = Path.home()
    source = Path(__file__).resolve().parent
    config = home / ".config/nvim-review"
    zshrc = home / ".zshrc"
    init = config / "init.lua"
    old_alias = (
        "alias review='NVIM_APPNAME=nvim-review nvim \"+DiffviewOpen --untracked-files=all\"'"
    )
    previous_function = """# Graphify review integration (Diffview keeps its original arguments).
review() {
  REVIEW_GRAPH_START=1 NVIM_APPNAME=nvim-review nvim "+DiffviewOpen --untracked-files=all" "$@"
}"""
    function = previous_function.replace("review() {", "unalias review 2>/dev/null\nreview() {")
    shell = zshrc.read_text()
    if old_alias not in shell and function not in shell and previous_function not in shell:
        raise SystemExit("review has changed; inspect .zshrc before installing")
    initial = init.read_text()
    marker = '\n-- Local file dependency graph for review (Graphify).\nrequire "review_graph"\n'
    command = shutil.which("review-graph")
    if command is None:
        command = str(Path(sys.executable).parent / "review-graph")
    if not Path(command).is_file():
        raise SystemExit("Install this package with uv tool install first")
    rendered = (
        (source / "neovim.lua").read_text().replace("__REVIEW_COMMAND__", json.dumps(command))
    )
    target = config / "lua/review_graph.lua"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path in (zshrc, init, target):
        if path.exists():
            backup = path.with_name(path.name + ".before-review-graph-" + stamp)
            shutil.copy2(path, backup)
            print(f"Backup: {backup}")
    shell = shell.replace(old_alias, function)
    if function not in shell:
        shell = shell.replace(previous_function, function)
    zshrc.write_text(shell)
    if marker not in initial:
        init.write_text(initial + marker)
    target.write_text(rendered)
    print(f"Installed {target}; command: {command}")


if __name__ == "__main__":
    install()
