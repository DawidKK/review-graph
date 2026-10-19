#!/usr/bin/env python3
"""Local Graphify file graph for a Git worktree; Python stdlib only."""

import argparse
import fcntl
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def changes(root: Path) -> dict:
    """Read both index columns, NUL-delimited paths and rename source paths."""
    records = iter(
        git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").split(
            b"\0"
        )
    )
    result = {}
    for record in records:
        if not record:
            continue
        xy, path = record[:2].decode(), os.fsdecode(record[3:])
        item = {
            "xy": xy,
            "staged": xy[0] not in " ?",
            "unstaged": xy[1] not in " ?",
            "untracked": xy == "??",
            "deleted": "D" in xy,
        }
        result[path] = item
        if "R" in xy or "C" in xy:
            old = os.fsdecode(next(records))
            item["old_path"] = old
            if "R" in xy:
                result[old] = {**item, "deleted": True, "renamed_to": path}
    return result


def aggregate(graph: dict, changed: dict, root: Path) -> dict:
    """Collapse only explicit source_file endpoints; keep one-hop incoming/outgoing edges."""
    if "nodes" not in graph or not ({"links", "edges"} & graph.keys()):
        raise ValueError("Unsupported Graphify format: expected nodes and links/edges")
    version = graph.get("graph", {}).get("schema_version")
    if version is not None and str(version).split(".")[0] != "1":
        raise ValueError(f"Unsupported Graphify schema_version: {version}")
    owners = {}
    for node in graph["nodes"]:
        source = node.get("source_file")
        if not source:
            continue
        path = (root / source).resolve()
        if not path.is_relative_to(root):
            continue
        owners[node["id"]] = path.relative_to(root).as_posix()
    grouped = defaultdict(list)
    visible = set(changed)
    for edge in graph.get("links", graph.get("edges", [])):
        source, target = owners.get(edge["source"]), owners.get(edge["target"])
        if not source or not target or source == target:
            continue
        if source not in changed and target not in changed:
            continue
        visible.update((source, target))
        # Keep evidence verbatim, including confidence, weight and source locations.
        grouped[source, target].append(edge)
    covered = set(owners.values())
    return {
        "nodes": [
            {
                "id": p,
                "changed": p in changed,
                "status": changed.get(p),
                "in_graph": p in covered,
            }
            for p in sorted(visible)
        ],
        "edges": [
            {"source": s, "target": t, "evidence": evidence}
            for (s, t), evidence in sorted(grouped.items())
        ],
        "metadata": graph.get("graph", {}),
        "total_graph_files": len(covered),
        "total_graph_nodes": len(graph["nodes"]),
        "unmapped_nodes": len(graph["nodes"]) - len(owners),
    }


class Review:
    def __init__(self, root: Path, state: Path, token: str):
        self.root, self.state, self.token = root, state, token
        self.condition = threading.Condition()
        self.pending = False
        self.busy = False
        self.error = None
        self.data = None
        self.revision = 0
        self.selection = {"revision": 0, "path": None}
        self.last_seen = 0
        self.open_after = False
        self.opened = False
        self.url = ""
        threading.Thread(target=self.worker, daemon=True).start()

    def request(self, action: str) -> None:
        with self.condition:
            if action == "open" and self.data:
                self.open_browser()
                return
            self.open_after = (
                self.open_after or not self.opened or action in {"open", "start"}
            )
            self.pending = True
            self.condition.notify()

    def open_browser(self) -> None:
        # A live page already polls this server: never create another tab on refresh.
        if time.monotonic() - self.last_seen > 15:
            subprocess.run(["open", "-g", self.url], check=True)
            self.last_seen = time.monotonic()
        self.opened = True

    def worker(self) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending)
                self.pending, self.busy, self.error = False, True, None
            try:
                # A fresh output avoids stale/deleted symbols and the smaller-graph guard.
                with tempfile.TemporaryDirectory(
                    prefix="build-", dir=self.state
                ) as output:
                    executable = shutil.which("graphify")
                    if not executable:
                        raise RuntimeError(
                            "graphify missing; run: uv tool install graphifyy"
                        )
                    with (self.state / "extract.log").open("w") as log:
                        subprocess.run(
                            [
                                executable,
                                "extract",
                                str(self.root),
                                "--code-only",
                                "--no-cluster",
                                "--out",
                                output,
                            ],
                            cwd=self.root,
                            stdout=log,
                            stderr=log,
                            check=True,
                            timeout=600,
                        )
                    # 0.9.91 uses <out>/graphify-out and edges; exported graphs may use links.
                    candidates = [
                        Path(output) / "graphify-out/graph.json",
                        Path(output) / "graph.json",
                    ]
                    source = next((p for p in candidates if p.exists()), None)
                    if source is None:
                        raise RuntimeError(
                            "Graphify produced no graph.json; see extract.log"
                        )
                    raw = source.read_bytes()
                    snapshot = aggregate(json.loads(raw), changes(self.root), self.root)
                    snapshot.update(
                        root=str(self.root),
                        generated=time.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    (self.state / "graph.json.tmp").write_bytes(raw)
                    (self.state / "graph.json.tmp").replace(self.state / "graph.json")
                    (self.state / "view.json.tmp").write_text(json.dumps(snapshot))
                    (self.state / "view.json.tmp").replace(self.state / "view.json")
                with self.condition:
                    self.data = snapshot
                    self.revision += 1
                    if self.open_after:
                        self.open_browser()
                        self.open_after = False
            except Exception as exc:
                with self.condition:
                    self.error = (
                        f"{type(exc).__name__}: {exc}. See {self.state / 'extract.log'}"
                    )
                print(self.error, file=sys.stderr, flush=True)
            finally:
                with self.condition:
                    self.busy = False


def create_server(root: Path, state: Path) -> tuple[ThreadingHTTPServer, Review]:
    token = secrets.token_urlsafe(32)
    review = Review(root, state, token)
    page = Path(__file__).with_name("view.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code: int, value: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(value)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(value)

        def authenticated(self) -> bool:
            host = self.headers.get("Host", "")
            return host == urlsplit(review.url).netloc and urlsplit(
                self.path
            ).path.startswith(f"/{token}/")

        def do_GET(self):
            if not self.authenticated():
                self.respond(404, b"Not found", "text/plain")
                return
            route = urlsplit(self.path).path.removeprefix(f"/{token}/")
            if route == "data":
                with review.condition:
                    review.last_seen = time.monotonic()
                    body = json.dumps(
                        {
                            "revision": review.revision,
                            "busy": review.busy,
                            "error": review.error,
                            "data": review.data,
                        }
                    ).encode()
                self.respond(200, body, "application/json")
            elif route == "":
                self.respond(200, page, "text/html; charset=utf-8")
            elif route == "selection":
                with review.condition:
                    review.last_seen = time.monotonic()
                    body = json.dumps(review.selection).encode()
                self.respond(200, body, "application/json")
            elif route == "health":
                self.respond(200, b"review-graph-v2", "text/plain")
            else:
                self.respond(404, b"Not found", "text/plain")

        def do_POST(self):
            route = urlsplit(self.path).path.rsplit("/", 1)[-1]
            if (
                not self.authenticated()
                or route not in {"refresh", "open", "start", "selection"}
                or self.headers.get("X-Review-Token") != token
            ):
                self.respond(403, b"Forbidden", "text/plain")
                return
            if route == "selection":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16384:
                        raise ValueError("Invalid selection length")
                    path = json.loads(self.rfile.read(length))["path"]
                    if path is not None and (
                        not isinstance(path, str)
                        or not path
                        or "\0" in path
                        or PurePosixPath(path).is_absolute()
                        or ".." in PurePosixPath(path).parts
                    ):
                        raise ValueError("Expected a repository-relative path")
                except ValueError, KeyError, TypeError:
                    self.respond(400, b"Invalid selection", "text/plain")
                    return
                with review.condition:
                    if path != review.selection["path"]:
                        review.selection = {
                            "revision": review.selection["revision"] + 1,
                            "path": path,
                        }
                self.respond(202, b"selected", "text/plain")
                return
            review.request(route)
            self.respond(202, b"queued", "text/plain")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    review.url = f"http://127.0.0.1:{server.server_port}/{token}/"
    info = {"url": review.url, "token": token, "pid": os.getpid(), "root": str(root)}
    (state / "server.json.tmp").write_text(json.dumps(info))
    (state / "server.json.tmp").replace(state / "server.json")
    return server, review


def serve(root: Path, state: Path) -> None:
    server, _ = create_server(root, state)
    server.serve_forever()


def connect(state: Path) -> dict | None:
    try:
        info = json.loads((state / "server.json").read_text())
        with urllib.request.urlopen(info["url"] + "health", timeout=1) as response:
            if response.read() == b"review-graph-v2":
                return info
    except OSError, ValueError, urllib.error.URLError:
        pass
    return None


def state_path(root: Path) -> Path:
    """Each worktree has private ignored state, including its selection channel."""
    return Path(
        os.fsdecode(
            git(
                root,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "review-graph",
            )
        ).strip()
    )


def send_selection(root: Path, path: str | None) -> bool:
    """Update an existing graph without starting analysis or opening a browser."""
    info = connect(state_path(root))
    if info is None:
        return False
    request = urllib.request.Request(
        info["url"] + "selection",
        data=json.dumps({"path": path}).encode(),
        headers={"X-Review-Token": info["token"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        response.read()
    return True


def start_server(root: Path, state: Path) -> tuple[subprocess.Popen, dict]:
    """Start a private process group, including any Graphify extraction children."""
    with (state / "server.log").open("a") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "review_graph.cli", "serve", str(root)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    try:
        for _ in range(100):
            info = connect(state)
            if info and info["pid"] == process.pid:
                return process, info
            if process.poll() is not None:
                raise RuntimeError(f"Server failed; see {state / 'server.log'}")
            time.sleep(0.05)
        raise RuntimeError(f"Server startup timed out; see {state / 'server.log'}")
    except BaseException:
        stop_process_group(process.pid)
        process.wait(timeout=5)
        raise


def stop_process_group(pid: int) -> None:
    """Stop the server and its children, never the caller's process group."""
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


class GraphSession:
    """The newest terminal owns a fresh server; older terminals cannot stop it."""

    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.closed = False
        self.process = None
        self.info = None

    def request(self, action: str) -> bool:
        # A cancelled Textual worker can leave a thread running: serialize startup,
        # requests and teardown so exiting cannot leave a late server behind.
        with self.lock:
            if self.closed:
                return False
            if self.info is None:
                state = state_path(self.root)
                state.mkdir(mode=0o700, parents=True, exist_ok=True)
                with (state / "startup.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    previous = connect(state)
                    if previous:
                        stop_process_group(previous["pid"])
                        for _ in range(100):
                            if connect(state) is None:
                                break
                            time.sleep(0.05)
                        else:
                            raise RuntimeError("Previous graph server did not stop")
                    self.process, self.info = start_server(self.root, state)
            request = urllib.request.Request(
                self.info["url"] + action,
                data=b"",
                headers={"X-Review-Token": self.info["token"]},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
            return True

    def close(self) -> None:
        with self.lock:
            self.closed = True
            if self.process is not None and self.process.poll() is None:
                stop_process_group(self.process.pid)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["refresh", "open", "start", "serve", "status"]
    )
    parser.add_argument("path", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(
        os.fsdecode(
            git(Path(args.path).resolve(), "rev-parse", "--show-toplevel")
        ).strip()
    )
    # git-path gives each worktree private, ignored state without touching .gitignore.
    state = state_path(root)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.action == "serve":
        serve(root, state)
        return
    with (state / "startup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        info = connect(state)
        if args.action == "status":
            print(json.dumps(info))
            return
        if not info:
            _, info = start_server(root, state)
    request = urllib.request.Request(
        info["url"] + args.action, data=b"", headers={"X-Review-Token": info["token"]}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()
    print(info["url"])


if __name__ == "__main__":
    main()
