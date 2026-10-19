# Review Graph

Review Graph helps you quickly review AI-generated code and see how the changed files fit into your system's architecture. It combines a side-by-side Git diff in the terminal with a dependency graph in your browser, showing which files import, call or reference one another. Selecting a file in the diff highlights its connections in the graph.

## Requirements

- **macOS**, **zsh** and **Homebrew** for the setup below.
- **Git** and **uv** to install and run the tool.
- **Python 3.14+** and **Graphify** for code analysis (installed below; tested with Graphify 0.9.91).

Textual, Rich and Pygments are installed automatically. Graph analysis runs locally without an AI API key.

## Setup

### 1. Install dependencies

```zsh
brew install git uv
uv python install 3.14
uv tool install 'graphifyy==0.9.91'
```

The package is named `graphifyy`; its command is `graphify`.

### 2. Clone and install Review Graph

```zsh
git clone https://github.com/DawidKK/review-graph
cd review-graph
uv sync --python 3.14
```

### 3. Add `review` to your shell

Add this to `~/.zshrc`, replacing any existing `review` alias or function:

```zsh
unalias review 2>/dev/null
review() {
  "$HOME/review-graph/.venv/bin/review" "$@"
}
```

If you cloned elsewhere, adjust the path in the function. Then reload your shell configuration and check Graphify:

```zsh
source ~/.zshrc
graphify --version
```

## Usage

Save your changes, then run inside any Git repository:

```zsh
review .
```

Or use `review /path/to/project`. The diff compares **HEAD** with your current files, including staged, unstaged and untracked changes. The browser opens after analysis and shows connections involving changed files; the tool does not edit or stage your code.

| Key | Action |
| --- | --- |
| `↑` / `↓`, `j` / `k` | Navigate files or scroll the diff, depending on focus. |
| `Tab` / `Enter` | Switch focus / focus the diff. |
| `[` / `]` | Jump to the previous / next changed range. |
| `r` | Reload saved changes and rebuild the graph. |
| `g` | Open the dependency graph. |
| `q` / `Esc` | Exit and stop the graph server. |

Each `review` session starts a fresh graph server. Use its new browser tab; older tabs can be closed. A terminal at least 120 columns wide works best.

## Update

```zsh
cd "$HOME/review-graph"
git pull
uv sync --python 3.14
```

Restart `review` to load the updated tool.
