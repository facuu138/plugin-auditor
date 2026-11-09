"""`cpa serve`: a live dashboard on localhost.

A watcher thread polls the transcript folder and plugin config every 2 seconds.
When anything changes, it rebuilds the audit and pushes it to every open page
over Server-Sent Events. Only changed transcripts are re-parsed.
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from .usage import TranscriptCache, transcript_files


ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


class State:
    def __init__(self, args) -> None:
        self.args = args
        self.home = Path(args.home).expanduser() if args.home else Path.home() / ".claude"
        self.cache = TranscriptCache()
        self.cond = threading.Condition()
        self.version = 0
        self.payload = b"{}"
        self.signature: tuple = ()
        self.lock = threading.Lock()  # refresh runs from the watcher and the risk scanner
        self.watched: list[Path] = []  # skill files from the last build, to notice edits
        self.skill_paths: list[str] = []
        self.risk_status = ""

    def _signature(self) -> tuple:
        """Cheap fingerprint of everything the audit reads."""
        files = transcript_files(self.home)
        extra = [self.home / "settings.json", self.home / "plugins" / "installed_plugins.json",
                 self.home / "plugin-auditor" / "usage.db",
                 self.home.parent / ".claude.json"]  # MCP servers and their per-project disabled lists
        sig = []
        for f in files + extra + self.watched:
            try:
                st = f.stat()
                sig.append((str(f), st.st_mtime, st.st_size))
            except OSError:
                pass
        return tuple(sig)

    def refresh(self, force: bool = False) -> bool:
        with self.lock:
            return self._refresh(force)

    def _refresh(self, force: bool) -> bool:
        sig = self._signature()
        if sig == self.signature and not force:
            return False
        from .cli import _filter, build  # late import: cli imports this module lazily

        data = build(self.args, self.cache, risk_status=self.risk_status)
        skills = [c for i in data["items"] for c in i["components"] if c["kind"] == "skill"]
        self.skill_paths = [c["path"] for c in skills]
        self.watched = [Path(p) for p in self.skill_paths]
        if self.args.source:
            data = _filter(data, self.args.source)
        text = json.dumps(data, default=list)
        demo = getattr(self.args, "demo", None)
        if demo:
            text = demo.rewrite(text)  # show fictional paths, not the temp folder
        payload = text.encode()
        with self.cond:
            self.signature = sig
            self.payload = payload
            self.version += 1
            self.cond.notify_all()
        return True

    def risk_worker(self, interval: float = 5.0) -> None:
        """Scan new or changed skills in the background. Each result updates the page."""
        from .risk import find_skillspector, scan_all, stale_folders

        if getattr(self.args, "demo", None):
            self.risk_status = "demo data"
            self.refresh(force=True)
            return
        if getattr(self.args, "no_risk", False):
            self.risk_status = "off (--no-risk)"
            return
        if not find_skillspector():
            self.risk_status = "SkillSpector not installed"
            self.refresh(force=True)
            return
        while True:
            try:
                stale = stale_folders(self.home, list(self.skill_paths))
                if stale:
                    def done(n, total):
                        self.risk_status = f"scanning {n}/{total}" if n < total else "up to date"
                        self.refresh(force=True)

                    self.risk_status = f"scanning 0/{len(stale)}"
                    self.refresh(force=True)
                    scan_all(self.home, list(self.skill_paths), progress=lambda *_: None, on_result=done)
                elif self.risk_status != "up to date":
                    self.risk_status = "up to date"
                    self.refresh(force=True)
            except Exception as exc:
                print(f"risk scan failed: {exc}", flush=True)
            time.sleep(interval)

    def watch(self, interval: float = 2.0) -> None:
        while True:
            time.sleep(interval)
            try:
                self.refresh()
            except Exception as exc:  # keep the server alive on a bad transcript line
                print(f"refresh failed: {exc}")


def _handler(state: State):
    page = resources.files("plugin_auditor").joinpath("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):  # quiet
            pass

        def _send(self, body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            # A web page can rebind its own domain to 127.0.0.1 and read our data. Only accept our own host names.
            if self.headers.get("Host", "").rsplit(":", 1)[0] not in ALLOWED_HOSTS:
                self.send_error(403)
                return
            if self.path in ("/", "/index.html"):
                self._send(page, "text/html; charset=utf-8")
            elif self.path == "/api/data":
                self._send(state.payload, "application/json")
            elif self.path == "/api/stream":
                self._stream()
            else:
                self.send_error(404)

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen = -1
            try:
                while True:
                    with state.cond:
                        state.cond.wait_for(lambda: state.version != seen, timeout=15)
                        version, payload = state.version, state.payload
                    if version != seen:
                        seen = version
                        self.wfile.write(b"data: " + payload + b"\n\n")
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def serve(args) -> int:
    state = State(args)
    print("Reading transcripts...", flush=True)
    state.refresh(force=True)
    threading.Thread(target=state.watch, daemon=True).start()
    threading.Thread(target=state.risk_worker, daemon=True).start()
    if getattr(args, "demo", None):
        def simulate():
            while True:
                time.sleep(4)
                args.demo.tick()
        threading.Thread(target=simulate, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), _handler(state))
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}"
    print(f"Dashboard: {url}  (updates live; Ctrl+C to stop)", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0
