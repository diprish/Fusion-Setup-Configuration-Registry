"""Local launcher page for the environment comparison runner.

Starts a small web server on 127.0.0.1 and opens a page where you pick the two
environments and the setup tasks, review the plan, approve the run, watch the
progress, and read the results. It only wraps compare_environments.py; it has
no Fusion authentication or REST code of its own.

Safety model
- Binds to 127.0.0.1 only. Every request must carry the session token printed at
  startup, and the Host header must be the loopback address (blocks DNS
  rebinding). No CORS headers are sent, so other web pages cannot call it.
- The runner is started with an argument list (no shell). Inputs are validated.
- Executing exports needs an explicit confirm flag from the page, which mirrors
  the runner's --execute approval gate. A plan never calls Fusion.
- One run at a time. Output is shown unredacted in your own browser so the
  sign-in URL stays usable; files written by the runner are redacted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compare_environments as ce  # noqa: E402

ROOT = ce.find_repo_root(HERE)
RUNNER = HERE / "compare_environments.py"
UI_FILE = HERE / "launcher_ui.html"
MAX_BODY = 64 * 1024
MAX_LINES = 20000
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class Job:
    def __init__(self, mode: str, cmd: list[str]):
        self.id = secrets.token_hex(4)
        self.mode, self.cmd = mode, cmd
        self.lines: list[str] = []
        self.status = "running"
        self.rc: int | None = None
        self.proc: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()

    def start(self) -> None:
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     start_new_session=hasattr(os, "setsid"))
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            with self.lock:
                if len(self.lines) < MAX_LINES:
                    self.lines.append(line.rstrip("\n"))
        self.rc = self.proc.wait()
        with self.lock:
            if self.status != "cancelled":
                self.status = "done"

    def cancel(self) -> None:
        if self.proc and self.proc.poll() is None:
            with self.lock:
                self.status = "cancelled"
            ce.terminate(self.proc)

    def snapshot(self, start: int) -> dict:
        with self.lock:
            return {"id": self.id, "mode": self.mode, "status": self.status, "rc": self.rc,
                    "lines": self.lines[start:], "next": len(self.lines)}


class State:
    allow_embed = False
    token = secrets.token_urlsafe(24)
    exporter: str | None = None
    port = 0
    job: Job | None = None
    lock = threading.Lock()


# ---------------------------------------------------------------- data helpers

def catalog_tree() -> dict:
    catalog = ce.load_catalog(ROOT)
    if not catalog:
        return {"offerings": [], "available": False}
    offerings = []
    for code, off in catalog.get("offerings", {}).items():
        fas = []
        for fa_code, fa in off.get("functionalAreas", {}).items():
            seen, tasks = set(), []
            for t in fa.get("tasks", []) or []:
                short = str(t.get("taskShortName", "")).upper()
                if not short or short in seen:
                    continue
                seen.add(short)
                tasks.append({"code": short, "name": t.get("taskName", ""), "csv": ce.norm_flag(t.get("csvExportImportSupportedFlag"))})
            fas.append({"code": fa_code, "name": fa.get("functionalAreaName", ""), "tasks": tasks})
        offerings.append({"code": code, "name": off.get("offeringName", ""), "functionalAreas": fas})
    return {"offerings": offerings, "available": True}


def find_properties() -> list[str]:
    sys.path.insert(0, str(ROOT / ce.PATHS_HELPER_DIR))
    from fusion_skill_paths import PROJECTS_ROOT  # noqa: E402
    bases = [PROJECTS_ROOT] + [Path(d) for d in os.environ.get("FSCR_PROPERTIES_DIRS", "").split(os.pathsep) if d]
    found: set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for p in base.rglob("*.properties"):
            if len(p.relative_to(base).parts) <= 6 and "exports" not in p.parts:
                found.add(str(p))
    return sorted(found)[:50]


def results_dir(project_id: str) -> Path:
    sys.path.insert(0, str(ROOT / ce.PATHS_HELPER_DIR))
    from fusion_skill_paths import project_artifacts_root  # noqa: E402
    return project_artifacts_root(project_id) / ce.RELATIVE_DIR


def build_command(body: dict) -> tuple[list[str], str]:
    mode = body.get("mode")
    if mode not in ("plan", "execute"):
        raise ValueError("mode must be plan or execute")
    if mode == "execute" and body.get("confirm") is not True:
        raise ValueError("Execute needs explicit approval (confirm)")
    project = str(body.get("project_id", "")).strip()
    if not PROJECT_RE.match(project):
        raise ValueError("Project id must be letters, digits, '.', '_' or '-'")
    cmd = [sys.executable, str(RUNNER), "--project-id", project]
    for side in ("baseline", "target"):
        entry = body.get(side) or {}
        label, props = str(entry.get("label", "")).strip(), str(entry.get("properties", "")).strip()
        if not ce.LABEL_RE.match(label):
            raise ValueError(f"{side}: label must be 1-30 letters, digits, '.', '_' or '-'")
        path = Path(props).expanduser()
        if path.suffix != ".properties" or not path.is_file():
            raise ValueError(f"{side}: properties file not found (must be an existing .properties file)")
        cmd += [f"--{side}-label", label, f"--{side}-properties", str(path)]
    tasks = [str(t).strip().upper() for t in body.get("tasks", []) if str(t).strip()]
    for t in tasks:
        if not ce.TASK_RE.match(t) or len(t) > 100:
            raise ValueError(f"Invalid task code: {t[:40]!r}")
        cmd += ["--task", t]
    for flag, key in (("--offering", "offering"), ("--functional-area", "functional_area")):
        value = str(body.get(key, "")).strip()
        if value:
            if len(value) > 100 or value.startswith("-"):
                raise ValueError(f"Invalid {key}")
            cmd += [flag, value]
    if not tasks and not body.get("offering") and not body.get("functional_area"):
        raise ValueError("Select at least one task or a functional area")
    for flag, key in (("--expected-col", "expected_col"), ("--expected-file", "expected_file"), ("--ignore-col", "ignore_col")):
        for v in body.get(key, []) or []:
            v = str(v).strip()
            if v:
                if len(v) > 200 or v.startswith("-"):
                    raise ValueError(f"Invalid value for {key}")
                try:
                    re.compile(v)
                except re.error as exc:
                    raise ValueError(f"{key}: not a valid regular expression ({exc})") from exc
                cmd += [flag, v]
    if body.get("replace"):
        cmd.append("--replace")
    if mode == "plan" and body.get("plan_json"):
        cmd.append("--plan-json")
    if mode == "execute":
        cmd.append("--execute")
        if body.get("refresh"):
            cmd.append("--refresh")
    if body.get("allow_many"):
        cmd.append("--allow-many")
    if State.exporter:
        cmd += ["--exporter", State.exporter]
    return cmd, mode


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "EnvCompareLauncher"

    def log_message(self, *_: object) -> None:  # quiet; the token must not reach logs
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        ancestors = "vscode-webview:" if State.allow_embed else "'none'"
        self.send_header("Content-Security-Policy", f"default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors {ancestors}")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: object) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in (f"127.0.0.1:{State.port}", f"localhost:{State.port}")

    def _authorized(self, query: dict) -> bool:
        supplied = self.headers.get("X-Launcher-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(supplied, State.token)

    def _guard(self, query: dict) -> bool:
        if not self._host_ok():
            self._json(403, {"error": "bad host"})
            return False
        if not self._authorized(query):
            self._json(403, {"error": "missing or wrong session token; use the URL printed by the launcher"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        if not self._guard(q):
            return
        try:
            if url.path == "/":
                self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/init":
                self._json(200, {"catalog": catalog_tree(), "properties": find_properties(), "repo": ROOT.name,
                                 "defaultMaxTasks": ce.DEFAULT_MAX_TASKS, "stubExporter": bool(State.exporter)})
            elif url.path == "/api/job":
                job = State.job
                if not job or job.id != (q.get("id") or [""])[0]:
                    self._json(404, {"error": "no such job"})
                else:
                    self._json(200, job.snapshot(int((q.get("from") or ["0"])[0] or 0)))
            elif url.path in ("/api/result", "/api/file"):
                project = (q.get("project_id") or [""])[0]
                if not PROJECT_RE.match(project):
                    self._json(400, {"error": "bad project id"})
                    return
                base = results_dir(project).resolve()
                if url.path == "/api/result":
                    f = base / ce.RESULT_NAME
                    if not f.is_file():
                        self._json(404, {"error": "no roll-up yet"})
                    else:
                        self._send(200, f.read_bytes(), "application/json")
                else:
                    rel = (q.get("rel") or [""])[0]
                    f = (base / rel).resolve()
                    if not rel or base not in f.parents or f.suffix not in (".md", ".json", ".log") or not f.is_file():
                        self._json(404, {"error": "not found"})
                    else:
                        self._send(200, f.read_bytes(), "text/plain; charset=utf-8")
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:  # keep the server alive; report a short message
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"[:300]})

    def do_POST(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        if not self._host_ok() or not secrets.compare_digest(self.headers.get("X-Launcher-Token", ""), State.token):
            self._json(403, {"error": "forbidden"})
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._json(415, {"error": "JSON only"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_BODY:
                self._json(413, {"error": "too large"})
                return
            body = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/run":
                with State.lock:
                    if State.job and State.job.status == "running":
                        self._json(409, {"error": "A run is already in progress"})
                        return
                    try:
                        cmd, mode = build_command(body)
                    except ValueError as exc:
                        self._json(400, {"error": str(exc)})
                        return
                    job = Job(mode, cmd)
                    job.start()
                    State.job = job
                self._json(200, {"id": job.id})
            elif url.path == "/api/cancel":
                if State.job:
                    State.job.cancel()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "bad request"})
        except Exception as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"[:300]})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Local launcher page for the environment comparison runner")
    p.add_argument("--port", type=int, default=0, help="Port on 127.0.0.1 (default: a free port)")
    p.add_argument("--no-browser", action="store_true", help="Print the URL but do not open a browser")
    p.add_argument("--exporter", help="Alternative exporter CLI (testing only)")
    p.add_argument("--allow-embed", action="store_true", help="Allow the page to be shown inside a VS Code webview")
    args = p.parse_args(argv)
    State.exporter = args.exporter
    State.allow_embed = args.allow_embed
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    State.port = server.server_address[1]
    url = f"http://127.0.0.1:{State.port}/?t={State.token}"
    print(f"Environment comparison launcher\n  {url}\nOnly this computer can reach it. Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if State.job:
            State.job.cancel()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
