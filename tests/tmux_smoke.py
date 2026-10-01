#!/usr/bin/env python3
"""End-to-end test of terminal mode (wowtmux.py) against stand-in servers.

Runs on a private tmux server in a temporary directory, so your own tmux
sessions are never touched. Stdlib only; needs tmux.

    python3 tests/tmux_smoke.py          # quiet unless something fails
    python3 tests/tmux_smoke.py -v       # show each check

Exit status is 0 when everything passed.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERBOSE = "-v" in sys.argv
FAILURES: list[str] = []
DB_PORT, AUTH_PORT = 47311, 47312


def port_up(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        if VERBOSE:
            print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name}{': ' + detail if detail else ''}")


PORT_SERVER = '''#!/usr/bin/env python3
"""Holds a port, like a database or auth server. FAIL_FIRST=n: the first n
starts of the auth stand-in exit at once, counted in FAIL_FILE."""
import os, signal, socket, sys, time
port = int(sys.argv[1])
count = os.environ.get("FAIL_FILE")
if count and port == %d:
    n = int(open(count).read()) if os.path.exists(count) else 0
    open(count, "w").write(str(n + 1))
    if n < int(os.environ.get("FAIL_FIRST", "0")):
        print("cannot reach the database", flush=True)
        sys.exit(1)
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen()
print("listening", flush=True)
while True:
    time.sleep(1)
''' % AUTH_PORT

WORLD_SERVER = '''#!/usr/bin/env python3
"""World stand-in: loads for 2 s, prints a ready line, obeys server shutdown."""
import signal, sys, time
signal.signal(signal.SIGTERM, lambda *a: (print("SIGTERM", flush=True), sys.exit(0)))
for i in range(2):
    time.sleep(1)
    print(f"loading {i}", flush=True)
print("World initialized", flush=True)
for line in sys.stdin:
    cmd = line.strip()
    print(f"> {cmd}", flush=True)
    if cmd.startswith("server shutdown"):
        time.sleep(int(cmd.split()[-1]))
        print("Halting process...", flush=True)
        sys.exit(0)
'''


def main() -> int:
    if subprocess.run(["which", "tmux"], capture_output=True).returncode != 0:
        print("tmux is not installed - nothing to test")
        return 1
    # tmux sockets live under TMUX_TMPDIR, and a Unix socket path has a
    # 108-byte limit, so keep the directory short.
    with tempfile.TemporaryDirectory(prefix="wat", dir="/tmp") as tmp:
        tmp = Path(tmp)
        (tmp / "bin").mkdir()
        (tmp / "tmux").mkdir()
        for name, body in (("port-server", PORT_SERVER), ("world-server", WORLD_SERVER)):
            path = tmp / "bin" / name
            path.write_text(body)
            path.chmod(0o755)
        cfg = tmp / "smoke.toml"
        cfg.write_text(f'''
[vars]
root = "{tmp}"
[app]
shutdown_delay = 1
shutdown_timeout = 20
log_dir = "{tmp}/state"
[realm]
name = "Smoke"
[servers.db]
display_name = "Database"
executable = "${{root}}/bin/port-server"
args = ["{DB_PORT}"]
working_dir = "${{root}}"
ready_port = {DB_PORT}
ready_timeout = 10
[servers.auth]
display_name = "Auth Server"
executable = "${{root}}/bin/port-server"
args = ["{AUTH_PORT}"]
working_dir = "${{root}}/bin"
ready_port = {AUTH_PORT}
ready_timeout = 10
retry_delay = 0.5
max_retries = 3
[servers.world]
display_name = "World Server"
executable = "${{root}}/bin/world-server"
working_dir = "${{root}}/bin"
console = true
ready_pattern = "World initialized"
ready_timeout = 30
''')
        env = {**os.environ, "TMUX_TMPDIR": str(tmp / "tmux"),
               "FAIL_FILE": str(tmp / "failcount"), "FAIL_FIRST": "2"}
        env.pop("TMUX", None)
        session = "wowadmin-smoke"
        outside: subprocess.Popen | None = None

        def run(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, str(ROOT / "wowtmux.py"), "--config", str(cfg), *args],
                                  capture_output=True, text=True, env=env, timeout=120)

        def tmux(*args: str) -> str:
            return subprocess.run(["tmux", *args], capture_output=True, text=True, env=env).stdout

        def states() -> dict[str, str]:
            out = run("status").stdout
            return {name: m.group(1) for name, label in (("db", "Database"), ("auth", "Auth Server"),
                                                         ("world", "World Server"))
                    if (m := re.search(rf"{label}\s+(\S+)", out))}

        def screen() -> str:
            return tmux("capture-pane", "-p", "-t", f"{session}:control")

        def wait_for(cond, seconds: float) -> bool:
            end = time.time() + seconds
            while time.time() < end:
                if cond():
                    return True
                time.sleep(0.3)
            return cond()

        try:
            res = run("up", "--no-attach")
            check("up creates the session", res.returncode == 0, res.stderr)
            windows = tmux("list-windows", "-t", session, "-F", "#{window_name}").split()
            check("one window per server plus control", windows == ["control", "db", "auth", "world"],
                  str(windows))
            check("everything starts stopped", set(states().values()) == {"stopped"}, str(states()))

            res = run("start")
            check("start all succeeds", res.returncode == 0, res.stdout + res.stderr)
            check("auth retried twice", res.stdout.count("retry") == 2, res.stdout)
            check("start order", res.stdout.index("starting Database") < res.stdout.index("starting Auth")
                  < res.stdout.index("starting World"))
            check("all running", set(states().values()) == {"running"}, str(states()))
            pid = int(tmux("display-message", "-p", "-t", f"{session}:world", "#{pane_pid}").strip())
            check("pane process is the server, not a shell",
                  b"world-server" in Path(f"/proc/{pid}/cmdline").read_bytes())

            res = run("stop", "world")
            check("world stops through its console", "sent 'server shutdown 1'" in res.stdout, res.stdout)
            check("world output reached its log",
                  "Halting process" in (tmp / "state" / "tmux-world.log").read_text())
            check("world now stopped", states().get("world") == "stopped", str(states()))

            res = run("restart", "auth")
            check("restart one server", res.returncode == 0 and states().get("auth") == "running",
                  res.stdout)

            run("stop")
            check("stop all", set(states().values()) == {"stopped"}, str(states()))

            # A database started by someone else: recognised, left alone on start,
            # and not mistaken for the auth server that shares its executable.
            outside = subprocess.Popen([str(tmp / "bin" / "port-server"), str(DB_PORT)],
                                       stdout=subprocess.DEVNULL, start_new_session=True, env=env)
            wait_for(lambda: port_up(DB_PORT), 5)
            st = states()
            check("outside server recognised", st.get("db") == "outside", str(st))
            check("shared executable not confused", st.get("auth") == "stopped", str(st))
            res = run("start")
            check("start leaves the outside server alone", "leaving it alone" in res.stdout
                  and outside.poll() is None, res.stdout)
            run("restart", "auth")
            check("restarting auth spares the outside database", outside.poll() is None)
            run("stop")
            check("stop all also stops the outside server", wait_for(lambda: outside.poll() is not None, 10))

            # The control window.
            check("control window draws", wait_for(lambda: "ACTIVITY" in screen(), 10), screen())
            tmux("send-keys", "-t", f"{session}:control", "S")
            check("S starts everything", wait_for(lambda: set(states().values()) == {"running"}, 40),
                  str(states()))
            tmux("send-keys", "-t", f"{session}:control", "3", "Enter")
            check("Enter opens the server's window",
                  wait_for(lambda: tmux("display-message", "-p", "-t", session, "#{window_name}").strip()
                           == "world", 5))
            tmux("select-window", "-t", f"{session}:control")
            tmux("send-keys", "-t", f"{session}:control", "x")
            check("stop asks first", wait_for(lambda: "[y/N]" in screen(), 5), screen())
            tmux("send-keys", "-t", f"{session}:control", "n")
            check("n cancels", wait_for(lambda: "cancelled" in screen(), 5) and states().get("world") == "running")
            tmux("send-keys", "-t", f"{session}:control", "X")
            time.sleep(0.5)
            tmux("send-keys", "-t", f"{session}:control", "y")
            check("X y stops everything", wait_for(lambda: set(states().values()) == {"stopped"}, 40), str(states()))

            res = run("down")
            gone = subprocess.run(["tmux", "has-session", "-t", session], capture_output=True,
                                  env=env).returncode != 0
            check("down closes the session", gone and "closed" in res.stdout, res.stdout)
        finally:
            if outside and outside.poll() is None:
                outside.send_signal(signal.SIGTERM)
            subprocess.run(["tmux", "kill-server"], capture_output=True, env=env)

    print("\nall checks passed" if not FAILURES else f"\n{len(FAILURES)} check(s) failed")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
