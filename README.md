# Review + Graphify

Local macOS helper for the existing NvChad `nvim-review` configuration. Python uses only
the standard library; Graphify runs in its own `uv tool` environment. No application
dependencies are changed.

Install once per computer, from any directory (macOS, Python 3.14+, uv, Git,
Graphify, and an existing NvChad `nvim-review` with Diffview are required):

```bash
uv tool install graphifyy
uv tool install ~/Documents/Projects/review-graph
review-graph-install-neovim
source ~/.zshrc
```

Then enter any Git project, including JavaScript projects, and run `review .`.
No files or Python dependencies are added to the reviewed project.

The installer backs up `.zshrc`, `nvim-review/init.lua`, and any installed
`lua/review_graph.lua`. It preserves Diffview arguments and only accepts the known
review alias/function; it refuses to overwrite an unfamiliar shell setup.
Neovim invokes the installed command, independent of the source checkout.
After changing this tool, update the installation with:

```bash
uv tool install --reinstall ~/Documents/Projects/review-graph
```

An editable installation (`uv tool install --editable /path/to/review-graph`) is
also available for developing the tool; that mode requires the checkout to remain in place.
To uninstall, restore the configuration backups and run `uv tool uninstall review-graph`.

| Command | Behaviour |
| --- | --- |
| `review .` | Open Diffview immediately; analyse the worktree in the background and open the graph when ready. |
| Space `g r` | Run `DiffviewRefresh` and queue a new Graphify analysis. |
| Space `g o` | Open the graph if no page is connected; reuse an existing connected page. |
| `review-graph refresh .` | Refresh without Neovim. |
| `review-graph status .` | Print server PID and URL, or `null` if stopped. |

Browser opening uses macOS `open -g`, so it does not activate the browser. Normal refreshes
never invoke it. The existing page polls every two seconds; it keeps its filter, selection
and zoom. There is one server per Git worktree, including simultaneous Neovim sessions.
Explicit open/start uses a 15-second page heartbeat to avoid duplicate tabs; a browser that
suspends a page longer than that can cause an explicit open/start to open another tab.
Background refreshes still never open tabs. File selection synchronization is not implemented.

## Data and scope

Verified with Graphify **0.9.91** and its official documentation:

- [Installation and official package](https://github.com/Graphify-Labs/graphify)
- [Local extraction](https://docs.graphify.com/reference/extract)
- [Documented JSON representation](https://docs.graphify.com/concepts/outputs)

Every refresh runs `graphify extract <root> --code-only --no-cluster --out <temporary-root>`.
This analyses current working files, including supported untracked source files, entirely
locally. Documents/media have no semantic extraction in this mode. Index-only versions of
files are not separately analysed. Save buffers first; neither the helper nor Diffview saves
buffers automatically.

The adapter accepts Graphify `nodes` plus `edges` or `links`, maps nodes by their explicit
`source_file`, drops edges without two repository file endpoints and edges within one file,
and groups directed edges by file pair. It keeps only pairs incident to a changed file:
incoming and outgoing, exactly one hop. It never manufactures import targets, transitive
links, historical links or links from proximity. Graphify confidence and original evidence
are available by clicking an edge. Hyperedges are not expanded into invented pairwise links.

Git uses `status --porcelain=v1 -z --untracked-files=all`: both index and worktree columns,
untracked paths, conflicts, deletions and rename source/destination names are retained.
Deleted/unsupported files appear even without Graphify nodes. Files without known edges are
shown as isolated nodes. Git status is sampled after extraction; edits during a build may
require another refresh for a consistent snapshot. Concurrent refresh requests are coalesced;
another analysis follows if a request arrives during the current build.

State and logs live at `git rev-parse --git-path review-graph` (normally `.git/review-graph`):

- `graph.json`: latest raw Graphify result;
- `view.json`: latest file-level graph and Git statuses;
- `extract.log`: most recent extraction warnings and output;
- `server.log`, `server.json`: background process diagnostics and local URL.

The server binds **127.0.0.1 only**, uses an unguessable URL, checks the Host header, requires
a token for mutations, and serves only its page/data endpoints. No CDN or external browser
requests are needed. Failed builds retain the previous graph and show the error. The process
stays alive after Neovim exits to keep the graph available. To stop it, send SIGTERM to the
PID printed by `status`; a later `review .` starts it again.

## Verification

```bash
PYTHONPATH=src python3.14 -m unittest discover -s tests -v
uvx ruff check src tests
uvx ruff format --check src tests
```

Tests create their own temporary Git repositories. They never stage, reset, restore or
commit files in the user's repository.
