#!/usr/bin/env python3
"""End-to-end smoke test: start a panel against stand-in servers and use it.

Stdlib only, no live realm needed. It exists because a panel that imports and
prints its banner can still fail on the first request — every endpoint has to
be called, not just parsed.

    python3 tests/smoke.py          # quiet unless something fails
    python3 tests/smoke.py -v       # show each check

Exit status is 0 when everything passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("SMOKE_PORT", "8199"))
BASE = f"http://127.0.0.1:{PORT}"
VERBOSE = "-v" in sys.argv

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        if VERBOSE:
            print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{': ' + detail if detail else ''}")
        print(f"  FAIL {name}{': ' + detail if detail else ''}")


def get(path: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            body = r.read()
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:                                  # noqa: BLE001
            return exc.code, None


def post(path: str, payload: dict) -> tuple[int, object]:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:                                  # noqa: BLE001
            return exc.code, None


STAND_IN = '''#!/usr/bin/env python3
"""Stand-in world server: announces itself, then obeys console commands."""
import sys, time
print("Stand-in starting", flush=True)
time.sleep(0.3)
print("World initialized", flush=True)
for line in sys.stdin:
    cmd = line.strip()
    print(f"[server] {cmd}", flush=True)
    if cmd.startswith("server shutdown"):
        print("[server] halting", flush=True)
        sys.exit(0)
'''

CONFIG = '''
[vars]
here = "{here}"
bot_pattern = "{bot_pattern}"

[app]
bind = "127.0.0.1"
port = {port}
autostart = false
browser = ""
log_dir = "{here}/state"
shutdown_delay = 1
shutdown_timeout = 30

[branding]
title = "Smoke Test"
console_hint = "type here"
[branding.theme]
accent = "#7fb3ff"

[realm]
name = "Smoke"
world_port = 8085

[database]
enabled = false
default_filter = "All Online"
[database.filters]
"All Online" = "SELECT 1"
"Online Bots" = {{ needs_bots = true, sql = "SELECT 2" }}

[servers.world]
display_name = "Stand-in World"
executable = "{here}/standin.py"
console = true
ready_pattern = "World initialized"
ready_timeout = 20.0
stop_console_command = "server shutdown {{delay}}"

[configs]
enabled = true
backup = false
[configs.files]
"notes.txt" = "{here}/notes.txt"
'''


def start_panel(cfg: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app.py"), "--config", str(cfg),
         "--no-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            code, _ = get("/api/state")
            if code == 200:
                return proc
        except OSError:
            pass
        if proc.poll() is not None:
            raise SystemExit(f"panel exited early:\n{proc.stdout.read()}")
        time.sleep(0.3)
    raise SystemExit("panel never answered")


def run(tmp: Path, bots: bool) -> None:
    label = "bots on" if bots else "bots off"
    print(f"[{label}]")
    here = tmp / label.replace(" ", "-")
    here.mkdir()
    (here / "standin.py").write_text(STAND_IN)
    (here / "standin.py").chmod(0o755)
    (here / "notes.txt").write_text("original\n")
    cfg = here / "wowadmin.toml"
    cfg.write_text(CONFIG.format(here=here, port=PORT,
                                 bot_pattern="RNDBOT%" if bots else ""))

    panel = start_panel(cfg)
    try:
        # --- the page and its state ---------------------------------------
        code, body = get("/")
        check("index.html served", code == 200 and b"<html" in bytes(body)[:200].lower())

        code, st = get("/api/state")
        check("/api/state", code == 200 and isinstance(st, dict))
        check("branding reaches the client",
              st["branding"]["title"] == "Smoke Test", str(st.get("branding")))
        check("realm summary built", st["realm_summary"].startswith("Smoke"))

        # --- the point of this pass ---------------------------------------
        if bots:
            check("bot filter offered", "Online Bots" in st["filters"],
                  str(st["filters"]))
        else:
            check("bot filter hidden", "Online Bots" not in st["filters"],
                  str(st["filters"]))
            code, _ = get("/api/db?filter=Online%20Bots")
            check("hidden filter refused", code == 400)
        check("non-bot filter always offered", "All Online" in st["filters"])
        check("default filter is one that is offered",
              st["default_filter"] in st["filters"])

        # --- lifecycle -----------------------------------------------------
        code, r = post("/api/start", {"server": "world"})
        check("start accepted", code == 200 and r.get("ok"), str(r))

        deadline = time.time() + 25
        while time.time() < deadline:
            _, st = get("/api/state")
            if st["servers"][0]["state"] == "running":
                break
            time.sleep(0.4)
        check("reaches running via ready_pattern",
              st["servers"][0]["state"] == "running", st["servers"][0]["state"])
        check("console reported ready", st["servers"][0]["console_ready"])

        code, hist = get("/api/history?server=world")
        check("history streams the log", code == 200 and
              any("World initialized" in l for l in hist["lines"]),
              str(hist)[:200])

        code, r = post("/api/command", {"command": "hello"})
        check("command accepted", code == 200 and r.get("ok"), str(r))
        time.sleep(1.5)
        _, hist = get("/api/history?server=world")
        check("command reached the server",
              any("[server] hello" in l for l in hist["lines"]))
        check("echo precedes the reply",
              next(i for i, l in enumerate(hist["lines"]) if l == "> hello") <
              next(i for i, l in enumerate(hist["lines"]) if "[server] hello" in l))

        # --- config editor --------------------------------------------------
        code, cfgs = get("/api/configs")
        check("/api/configs", code == 200 and "notes.txt" in cfgs["files"])
        code, f = get("/api/config?name=notes.txt")
        check("/api/config reads", code == 200 and f["content"] == "original\n")
        code, r = post("/api/config-save",
                       {"name": "notes.txt", "content": "edited\n",
                        "mtime": f["mtime"]})
        check("config saves", code == 200 and r.get("ok"), str(r))
        check("file really changed",
              (here / "notes.txt").read_text() == "edited\n")
        code, r = post("/api/config-save",
                       {"name": "notes.txt", "content": "clobber\n",
                        "mtime": 1})
        check("stale mtime refused", code == 400)
        code, _ = get("/api/config?name=/etc/passwd")
        check("unknown config name refused", code == 400)
        code, _ = get("/static/../app.py")
        check("static traversal refused", code == 404)

        # --- graceful stop --------------------------------------------------
        code, r = post("/api/stop", {"server": "world"})
        check("stop accepted", code == 200 and r.get("ok"), str(r))
        _, hist = get("/api/history?server=world")
        check("stopped through its own console",
              any("server shutdown" in l for l in hist["lines"]))
        _, st = get("/api/state")
        check("reports stopped", st["servers"][0]["state"] == "stopped")
    finally:
        panel.terminate()
        try:
            panel.wait(timeout=10)
        except subprocess.TimeoutExpired:
            panel.kill()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wowadmin-smoke-") as d:
        tmp = Path(d)
        run(tmp, bots=True)
        run(tmp, bots=False)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
