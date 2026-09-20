#!/usr/bin/env python3
"""wowadmin — a control panel for a local World of Warcraft emulator realm.

Core-agnostic: every path, port, query, readiness signal and piece of branding
comes from a TOML file, so the same code drives CMaNGOS Classic, AzerothCore,
TrinityCore or a fork without edits. See wowadmin.example.toml.

Stdlib only. Console output is a one-way stream, so it goes to the browser over
Server-Sent Events rather than WebSockets; commands and control are ordinary
POSTs. That keeps the whole thing dependency-free.

The world server is started with a real stdin pipe, so commands go straight to
its console prompt — no FIFO, and no risk of the console seeing EOF and
shutting the server down.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
import webbrowser
from collections import deque

try:
    import psutil
except ImportError:                                   # pragma: no cover
    psutil = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
LOOPBACK = {"127.0.0.1", "::1", "localhost", "127.0.1.1"}


# --- configuration ---------------------------------------------------------

def config_candidates(explicit: str | None) -> list[Path]:
    """Where to look for wowadmin.toml, in order of precedence."""
    if explicit:
        return [Path(explicit).expanduser()]
    out = []
    env = os.environ.get("WOWADMIN_CONFIG")
    if env:
        out.append(Path(env).expanduser())
    out.append(Path.cwd() / "wowadmin.toml")
    out.append(BASE / "wowadmin.toml")
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    out.append(Path(xdg).expanduser() / "wowadmin" / "wowadmin.toml")
    return out


def find_config(explicit: str | None) -> Path:
    tried = config_candidates(explicit)
    for path in tried:
        if path.is_file():
            return path
    listing = "\n  ".join(str(p) for p in tried)
    raise SystemExit(f"no config file found. Looked for:\n  {listing}\n"
                     f"Copy wowadmin.example.toml and pass it with --config.")


def expand(value, vars: dict[str, str], seen: tuple[str, ...] = ()):
    """Substitute ${name} from [vars], then the environment, everywhere.

    One mechanism covers both path roots and query fragments, so a user sets
    ${root} once and it reaches executables, args, stop commands and SQL alike.
    An unknown name is left alone rather than blanked: a literal ${...} in the
    output is a visible mistake, an empty string is a silent one.
    """
    if isinstance(value, dict):
        return {k: expand(v, vars, seen) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, vars, seen) for v in value]
    if not isinstance(value, str):
        return value

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in seen:                      # ${a} = "${b}", ${b} = "${a}"
            print(f"wowadmin: circular variable ${{{name}}} — left as-is")
            return m.group(0)
        if name in vars:
            return str(expand(vars[name], vars, seen + (name,)))
        if name in os.environ:
            return os.environ[name]
        print(f"wowadmin: unknown variable ${{{name}}} — left as-is")
        return m.group(0)

    return VAR.sub(sub, value)


def load_config(path: Path) -> dict:
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    vars = dict(cfg.get("vars", {}))
    # Built-ins, overridable: a config that lives next to the server install
    # can say ${config_dir}/../bin and never mention an absolute path.
    vars.setdefault("config_dir", str(path.parent))
    vars.setdefault("home", str(Path.home()))
    expanded = {k: expand(v, vars) for k, v in cfg.items() if k != "vars"}
    expanded["vars"] = vars
    return expanded


def resolve_exe(raw: str) -> str:
    """A path (with ~ and $VARS) or a bare command name looked up on PATH."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if os.sep in expanded:
        return os.path.abspath(expanded)
    return shutil.which(expanded) or expanded


ARGS = argparse.ArgumentParser(description="WoW emulator realm control panel")
ARGS.add_argument("--config", "-c", help="path to wowadmin.toml")
ARGS.add_argument("--no-browser", action="store_true",
                  help="do not open a browser on startup")
ARGS.add_argument("--print-config", action="store_true",
                  help="dump the resolved config and exit")
OPTS = ARGS.parse_args()

CONFIG_PATH = find_config(OPTS.config)
CFG = load_config(CONFIG_PATH)
APP = CFG.get("app", {})
MAX_LINES = int(APP.get("log_max_lines", 5000))


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


class Managed:
    """One supervised process: its output ring buffer and its subscribers."""

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.display = spec.get("display_name", name)
        self.wants_console = bool(spec.get("console", False))
        # manage = false: a database run as a system service, say. The panel
        # reports on it and the roster still works, but it never starts or
        # stops something it does not own.
        self.managed = bool(spec.get("manage", True))
        self.retries = 0
        self.retry_pending = False
        self.exe = resolve_exe(spec.get("executable", ""))
        self.cwd = (os.path.expanduser(spec.get("working_dir", ""))
                    or os.path.dirname(self.exe))
        self.proc: subprocess.Popen | None = None
        self.lines: deque[str] = deque(maxlen=MAX_LINES)
        self.subscribers: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.started_at: float | None = None
        self.state = "stopped"          # stopped | starting | running | stopping
        self.last_error: str = ""
        # A server we did not spawn (started from a terminal, or left behind
        # when the panel restarted). We can report and stop it, but its stdout
        # belongs to whoever launched it, so there is no console for it.
        self.adopted_pid: int | None = None

    # -- output fan-out ----------------------------------------------------

    def emit(self, line: str) -> None:
        line = ANSI.sub("", line.rstrip("\n"))
        with self.lock:
            self.lines.append(line)
            dead = []
            for q in self.subscribers:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.subscribers.remove(q)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def snapshot(self) -> list[str]:
        with self.lock:
            return list(self.lines)

    # -- lifecycle ---------------------------------------------------------

    def owns_process(self) -> bool:
        """True when this panel spawned the process (so it has a console)."""
        return self.proc is not None and self.proc.poll() is None

    def _scan_external(self) -> int | None:
        """Find a live process running our executable that we didn't spawn.

        Two realms of the same core on one box run the same binary, so the
        executable alone is not identity: the candidate must also sit in our
        working directory (or, failing that, own our readiness port). Without
        that check each panel happily adopts the other realm's world server
        and offers to stop it.
        """
        if psutil is None:
            return None
        exe = os.path.realpath(self.exe)
        if not exe:
            return None
        want_cwd = os.path.realpath(self.cwd) if self.cwd else ""
        loose: int | None = None
        for p in psutil.process_iter(["pid", "exe", "name"]):
            try:
                pexe = p.info.get("exe")
                if not pexe or os.path.realpath(pexe) != exe:
                    continue
                if not want_cwd:
                    return p.info["pid"]
                try:
                    if os.path.realpath(p.cwd()) == want_cwd:
                        return p.info["pid"]
                except (psutil.AccessDenied, OSError):
                    # cwd unreadable (different user, or a kernel thread):
                    # remember it, but keep looking for a confident match.
                    loose = loose or p.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return loose

    def refresh(self) -> None:
        """Reconcile state with reality before reporting it."""
        if self.owns_process():
            self.adopted_pid = None
            return
        if self.adopted_pid is not None:
            if psutil is not None and psutil.pid_exists(self.adopted_pid):
                return
            self.adopted_pid = None
        found = self._scan_external()
        if found:
            self.adopted_pid = found
            if self.state in ("stopped", "starting"):
                self.state = "running"
        elif self.state not in ("starting", "stopping"):
            self.state = "stopped"

    def is_running(self) -> bool:
        return self.owns_process() or self.adopted_pid is not None

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            self.emit(raw)
        code = self.proc.wait()
        self.emit(f"*** {self.display} exited (code {code}) ***")
        was_starting = self.state == "starting"
        self.state = "stopped"
        self.started_at = None

        # A server that dies before it is ready usually lost a race with its
        # database. Retry, but only during startup and only if asked: a crash
        # after a good start is news, not something to paper over, and a
        # deliberate stop must stay stopped.
        delay = float(self.spec.get("retry_delay", 0) or 0)
        limit = int(self.spec.get("max_retries", 0) or 0)
        if code != 0 and was_starting and delay > 0 and self.retries < limit:
            self.retries += 1
            self.retry_pending = True
            # Still "starting", not "stopped": the panel is mid-attempt, and
            # start_all must keep waiting rather than move on to the next
            # server while this one's database connection is still missing.
            self.state = "starting"
            self.emit(f"*** retrying in {delay:g}s "
                      f"({self.retries}/{limit}) ***")
            threading.Timer(delay, self._retry_start).start()

    def _retry_start(self) -> None:
        """A scheduled retry, cancelled simply by clearing the flag."""
        if self.retry_pending:
            self.start()

    def start(self) -> tuple[bool, str]:
        self.refresh()
        if not self.managed:
            return False, "monitored only — start it yourself"
        if self.is_running():
            return False, "already running"
        exe = self.exe
        if not exe:
            return False, f"no executable configured for {self.name}"
        if not os.path.isfile(exe) or not os.access(exe, os.X_OK):
            self.retry_pending = False
            return False, f"not executable: {exe}"

        self.state = "starting"
        self.retry_pending = False
        self.last_error = ""
        cwd = self.cwd or os.path.dirname(exe)
        env = os.environ.copy()
        env.update({k: str(v) for k, v in self.spec.get("env", {}).items()})
        self.emit(f"*** starting {self.display} ***")
        try:
            self.proc = subprocess.Popen(
                [exe, *self.spec.get("args", [])],
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE if self.wants_console else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            self.state = "stopped"
            self.retry_pending = False
            self.last_error = str(exc)
            self.emit(f"*** failed to start: {exc} ***")
            return False, str(exc)

        self.started_at = time.time()
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._await_ready, daemon=True).start()
        return True, "starting"

    def _await_ready(self) -> None:
        """Flip to 'running' once the readiness signal appears."""
        timeout = float(self.spec.get("ready_timeout", 60.0))
        pattern = self.spec.get("ready_pattern")
        rport = self.spec.get("ready_port")
        deadline = time.time() + timeout
        rx = re.compile(pattern) if pattern else None

        while time.time() < deadline:
            if not self.is_running():
                return
            if rport and port_open(int(rport)):
                break
            if rx and any(rx.search(l) for l in self.snapshot()[-400:]):
                break
            if not rport and not rx:
                break
            time.sleep(0.5)

        if self.is_running():
            self.state = "running"
            self.emit(f"*** {self.display} ready ***")

    def send(self, text: str) -> tuple[bool, str]:
        if not self.wants_console:
            return False, "this process has no console"
        if self.adopted_pid is not None and not self.owns_process():
            return False, ("this server was started outside the panel — "
                           "restart it here to use the console")
        if not self.owns_process() or not self.proc or not self.proc.stdin:
            return False, "not running"
        # Echo before writing: the server can answer faster than this thread
        # gets to run again, and a reply printed above its own command makes
        # the transcript lie about the order.
        self.emit(f"> {text}")
        try:
            self.proc.stdin.write(text.rstrip("\n") + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            return False, str(exc)
        return True, "sent"

    def _signal(self, sig) -> None:
        """Signal whichever process we have — ours or an adopted one."""
        if self.owns_process() and self.proc:
            try:
                os.killpg(os.getpgid(self.proc.pid), sig)
                return
            except OSError:
                try:
                    self.proc.send_signal(sig)
                    return
                except OSError:
                    pass
        if self.adopted_pid:
            try:
                os.kill(self.adopted_pid, sig)
            except OSError:
                pass

    def stop(self) -> tuple[bool, str]:
        self.refresh()
        if not self.managed:
            return False, "monitored only — stop it yourself"
        if not self.is_running():
            # Between attempts there is no process to signal, but the panel
            # has still been told to stop: drop the scheduled retry.
            if self.retry_pending:
                self.retry_pending = False
                self.retries = 0
                self.state = "stopped"
                self.emit("*** retry cancelled ***")
                return True, "stopped"
            return False, "not running"
        adopted = not self.owns_process()
        self.state = "stopping"
        self.emit(f"*** stopping {self.display}"
                  f"{' (started outside the panel)' if adopted else ''} ***")
        timeout = float(APP.get("shutdown_timeout", 300))

        # Preferred: the server's own graceful path.
        if self.wants_console and not adopted:
            template = self.spec.get("stop_console_command",
                                     "server shutdown {delay}")
            delay = int(APP.get("shutdown_delay", 1))
            try:
                self.send(template.format(delay=delay))
            except (KeyError, IndexError) as exc:
                self.emit(f"*** stop_console_command is not a valid template "
                          f"({exc}) — falling back to SIGTERM ***")
                self._signal(signal.SIGTERM)
        elif self.spec.get("stop_command"):
            try:
                subprocess.run([resolve_exe(self.spec["stop_command"][0]),
                                *self.spec["stop_command"][1:]], timeout=60,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as exc:                      # noqa: BLE001
                self.emit(f"*** stop command failed: {exc} ***")
        else:
            # Emulator cores shut down cleanly on SIGTERM, which is the only
            # graceful lever we have over an adopted process.
            self._signal(signal.SIGTERM)

        deadline = time.time() + timeout
        while time.time() < deadline:
            self.refresh()
            if not self.is_running():
                break
            time.sleep(0.5)

        self.refresh()
        if self.is_running():
            self.emit("*** graceful stop timed out — terminating ***")
            self._signal(signal.SIGTERM)
            for _ in range(10):
                time.sleep(0.5)
                self.refresh()
                if not self.is_running():
                    break
        self.refresh()
        if self.is_running():
            self.emit("*** still alive — killing ***")
            self._signal(signal.SIGKILL)
            time.sleep(1)
            self.refresh()

        self.state = "stopped"
        self.started_at = None
        self.adopted_pid = None
        self.retry_pending = False
        self.retries = 0
        return True, "stopped"

    def status(self) -> dict:
        self.refresh()
        running = self.is_running()
        owned = self.owns_process()
        pid = self.proc.pid if owned and self.proc else self.adopted_pid
        uptime = int(time.time() - self.started_at) if owned and self.started_at else 0
        if running and not owned and psutil is not None and pid:
            try:
                uptime = int(time.time() - psutil.Process(pid).create_time())
            except Exception:                              # noqa: BLE001
                uptime = 0
        return {
            "name": self.name,
            "display": self.display,
            "state": self.state if running or self.state == "stopping" else "stopped",
            "running": running,
            "adopted": running and not owned,
            "pid": pid,
            "uptime": uptime,
            "console": self.wants_console,
            "managed": self.managed,
            "error": self.last_error,
        }


# Ordered as declared in the TOML, e.g. database -> auth -> world.
SERVERS: dict[str, Managed] = {
    name: Managed(name, spec)
    for name, spec in CFG.get("servers", {}).items()
    if spec.get("enabled", True)
}
ORDER = list(SERVERS)


def start_all() -> None:
    """Start in order, waiting for each to report ready before the next."""
    for name in ORDER:
        srv = SERVERS[name]
        if srv.is_running() or not srv.managed:
            continue
        srv.retries = 0
        srv.start()
        # Allow for the retry budget as well, or the chain moves on while a
        # server is still between attempts.
        budget = (float(srv.spec.get("retry_delay", 0) or 0)
                  * int(srv.spec.get("max_retries", 0) or 0))
        deadline = time.time() + float(srv.spec.get("ready_timeout", 60.0)) + budget
        while (time.time() < deadline and srv.state == "starting"
               and (srv.is_running() or srv.retry_pending)):
            time.sleep(0.5)

        # Starting a world server whose database just died only buries the
        # real error under a second one. Stop the chain and say so where the
        # user is already looking.
        srv.refresh()
        if not srv.is_running():
            srv.emit(f"*** {srv.display} did not come up — "
                     f"not starting the rest ***")
            for later in ORDER[ORDER.index(name) + 1:]:
                SERVERS[later].emit(f"*** skipped: {srv.display} "
                                    f"did not start ***")
            return


def stop_all() -> None:
    for name in reversed(ORDER):
        if SERVERS[name].is_running() and SERVERS[name].managed:
            SERVERS[name].stop()


# --- database panel --------------------------------------------------------

DB = CFG.get("database", {})
DB_CLIENT = resolve_exe(DB.get("client", ""))


def db_probe_port() -> int | None:
    """The port to probe for "is the database up?".

    Explicit [database].port wins. Otherwise borrow the readiness port of the
    managed server named by [database].server, falling back to the first
    managed server that has one — so renaming [servers.mysql] cannot silently
    break the probe.
    """
    if DB.get("port"):
        return int(DB["port"])
    named = DB.get("server")
    specs = CFG.get("servers", {})
    if named and specs.get(named, {}).get("ready_port"):
        return int(specs[named]["ready_port"])
    for spec in specs.values():
        if spec.get("ready_port") and not spec.get("console"):
            return int(spec["ready_port"])
    return None


DB_PORT = db_probe_port()


def db_online() -> bool:
    # With no port to probe we cannot pre-check; let the query itself answer.
    return True if DB_PORT is None else port_open(DB_PORT,
                                                 DB.get("host", "127.0.0.1"))


def db_command(sql: str) -> tuple[list[str], dict[str, str]]:
    """Build the client argv and the extra env for one query.

    Two styles are supported: a --defaults-file (credentials stay in a file
    only the user can read) or explicit host/port/user. The password is only
    ever passed through MYSQL_PWD — never in argv, where every user on the box
    could read it out of /proc.
    """
    cmd = [DB_CLIENT]
    env: dict[str, str] = {}
    if DB.get("defaults_file"):
        cmd.append(f"--defaults-file={os.path.expanduser(DB['defaults_file'])}")
    else:
        if DB.get("socket"):
            cmd += ["--protocol=SOCKET", f"--socket={DB['socket']}"]
        else:
            cmd += [f"--host={DB.get('host', '127.0.0.1')}"]
            if DB.get("port"):
                cmd.append(f"--port={int(DB['port'])}")
        if DB.get("user"):
            cmd.append(f"--user={DB['user']}")
        if DB.get("password"):
            env["MYSQL_PWD"] = str(DB["password"])
    if DB.get("database"):
        cmd.append(f"--database={DB['database']}")
    cmd += ["--batch", "--raw", "-e", sql]
    return cmd, env


def run_query(sql: str) -> dict:
    if not DB.get("enabled", False) or not os.path.isfile(DB_CLIENT):
        return {"error": "database client not configured"}
    if not db_online():
        # Expected whenever the database is simply stopped — not worth
        # surfacing as an error the user can do nothing about.
        return {"offline": True, "columns": [], "rows": []}
    cmd, extra = db_command(sql)
    env = {**os.environ, **extra} if extra else None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                             env=env)
    except subprocess.TimeoutExpired:
        return {"error": "query timed out"}
    if out.returncode != 0:
        msg = (out.stderr or "query failed").strip().splitlines()[-1][:300]
        # The port was open a moment ago but the server went away mid-query,
        # or is still starting up. Same situation, same quiet handling.
        if any(code in msg for code in ("2002", "2003", "2013")):
            return {"offline": True, "columns": [], "rows": []}
        return {"error": msg}
    rows = [r.split("\t") for r in out.stdout.rstrip("\n").split("\n") if r]
    if not rows:
        return {"columns": [], "rows": []}
    return {"columns": rows[0], "rows": rows[1:]}


def population() -> dict:
    """The one-number online count for the header, from [database].count_query."""
    sql = DB.get("count_query")
    if not sql or not DB.get("enabled", False):
        return {}
    res = run_query(sql)
    if res.get("rows"):
        return {"label": (res["columns"] or ["Online"])[0],
                "value": res["rows"][0][0]}
    return {}


# --- branding --------------------------------------------------------------

BRAND = CFG.get("branding", {})
# Image assets are resolved by NAME, exactly like editable configs: the browser
# asks for /api/asset?name=logo and the server looks the path up here, so a
# user's artwork can live anywhere without the panel accepting paths.
ASSET_KEYS = ("logo", "banner", "favicon", "apple_icon")
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".svg": "image/svg+xml", ".gif": "image/gif",
        ".ico": "image/x-icon", ".avif": "image/avif"}


def asset_path(name: str) -> Path | None:
    raw = BRAND.get(name) if name in ASSET_KEYS else None
    if not raw:
        return None
    p = Path(os.path.expanduser(str(raw)))
    # A relative setting is relative to the config file, which is where a
    # per-install theme folder naturally sits.
    return p if p.is_absolute() else (CONFIG_PATH.parent / p)


def realm_summary() -> str:
    """The header line, from whichever realm keys the user actually set.

    An explicit [realm].summary wins. Otherwise the pieces present are joined,
    so a core without a separate auth port simply shows one port instead of
    the word "undefined".
    """
    realm = CFG.get("realm", {})
    if realm.get("summary"):
        return str(realm["summary"])
    bits = []
    if realm.get("name"):
        bits.append(str(realm["name"]))
    if realm.get("realm_port"):
        bits.append(f"realm {realm['realm_port']}")
    if realm.get("world_port"):
        bits.append(f"world {realm['world_port']}")
    if realm.get("server_build"):
        bits.append(str(realm["server_build"]))
    return " · ".join(bits)


def branding_payload() -> dict:
    return {
        "title": BRAND.get("title") or CFG.get("realm", {}).get("name")
                 or "wowadmin",
        "logo": "/api/asset?name=logo" if asset_path("logo") else None,
        "logo_alt": BRAND.get("logo_alt", ""),
        "logo_height": BRAND.get("logo_height", 46),
        "banner": "/api/asset?name=banner" if asset_path("banner") else None,
        "banner_opacity": BRAND.get("banner_opacity", 0.30),
        "banner_position": BRAND.get("banner_position", "center 38%"),
        "favicon": "/api/asset?name=favicon" if asset_path("favicon") else None,
        "apple_icon": ("/api/asset?name=apple_icon"
                       if asset_path("apple_icon") else None),
        "theme": BRAND.get("theme", {}),
        "console_hint": BRAND.get("console_hint", ""),
    }


# --- config editor ---------------------------------------------------------

CONFIGS = CFG.get("configs", {})


def config_path(name: str) -> Path | None:
    """Resolve an editable file by NAME only.

    The browser never supplies a path, so there is nothing to traverse: an
    unknown name simply has no entry here.
    """
    if not CONFIGS.get("enabled", False):
        return None
    raw = CONFIGS.get("files", {}).get(name)
    return Path(os.path.expanduser(raw)) if raw else None


def read_config(name: str) -> dict:
    path = config_path(name)
    if path is None:
        return {"error": "unknown or disabled config"}
    if not path.is_file():
        return {"error": f"missing on disk: {path}"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"error": str(exc)}
    st = path.stat()
    return {"name": name, "path": str(path), "content": text,
            "mtime": int(st.st_mtime), "bytes": st.st_size,
            "syntax": "toml" if path.suffix == ".toml" else "ini"}


def write_config(name: str, content: str, mtime: int | None) -> dict:
    path = config_path(name)
    if path is None:
        return {"error": "unknown or disabled config"}
    if not path.is_file():
        return {"error": f"missing on disk: {path}"}

    # Refuse to clobber an edit made outside the panel since this tab loaded.
    if mtime and int(path.stat().st_mtime) != int(mtime):
        return {"error": "file changed on disk since you opened it — reload first"}

    if CONFIGS.get("backup", True):
        bak = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            shutil.copy2(path, bak)
        except OSError as exc:
            return {"error": f"backup failed, nothing written: {exc}"}

    # Write via a temp file in the same directory, then rename: a crash midway
    # leaves the original intact rather than a half-written config.
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return {"error": str(exc)}

    return {"ok": True, "mtime": int(path.stat().st_mtime),
            "bytes": path.stat().st_size}


# --- HTTP ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wowadmin"

    def log_message(self, *args):        # keep the panel's own console quiet
        pass

    # helpers
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:                                   # noqa: N802
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)

        if path in ("/", "/index.html"):
            f = BASE / "static" / "index.html"
            return self._send(200, f.read_bytes(), "text/html; charset=utf-8")

        if path.startswith("/static/"):
            # Resolve inside static/ and verify containment before reading.
            rel = path[len("/static/"):]
            target = (BASE / "static" / rel).resolve()
            root = (BASE / "static").resolve()
            if not str(target).startswith(str(root) + os.sep) or not target.is_file():
                return self._json({"error": "not found"}, 404)
            ctype = MIME.get(target.suffix, {".css": "text/css",
                             ".js": "text/javascript"}.get(target.suffix,
                             "application/octet-stream"))
            return self._send(200, target.read_bytes(), ctype)

        if path == "/api/asset":
            target = asset_path(qs.get("name", [""])[0])
            if target is None or not target.is_file():
                return self._json({"error": "not found"}, 404)
            return self._send(200, target.read_bytes(),
                              MIME.get(target.suffix.lower(),
                                       "application/octet-stream"))

        if path == "/api/state":
            filters = list(DB.get("filters", {}))
            default = DB.get("default_filter", "")
            return self._json({
                "servers": [SERVERS[n].status() for n in ORDER],
                "realm": CFG.get("realm", {}),
                "realm_summary": realm_summary(),
                "branding": branding_payload(),
                "population": population(),
                "log_max_lines": MAX_LINES,
                "poll_interval": float(APP.get("poll_interval", 3.0)),
                "db_interval": float(DB.get("interval", 15.0)),
                "filters": filters,
                # Fall back to the first filter if default_filter names one
                # that no longer exists, so a typo cannot blank the panel.
                "default_filter": default if default in filters
                                  else (filters[0] if filters else ""),
            })

        if path == "/api/history":
            name = qs.get("server", [""])[0]
            if name not in SERVERS:
                return self._json({"error": "unknown server"}, 404)
            return self._json({"lines": SERVERS[name].snapshot()})

        if path == "/api/db":
            if not DB.get("enabled", False):
                return self._json({"error": "database panel disabled"}, 400)
            filters = DB.get("filters", {})
            want = qs.get("filter", [DB.get("default_filter", "")])[0]
            if want not in filters:
                return self._json({"error": f"unknown filter {want!r}"}, 400)
            return self._json(run_query(filters[want]))

        if path == "/api/configs":
            return self._json({
                "enabled": bool(CONFIGS.get("enabled", False)),
                "files": list(CONFIGS.get("files", {})),
            })

        if path == "/api/config":
            res = read_config(qs.get("name", [""])[0])
            return self._json(res, 200 if "error" not in res else 400)

        if path == "/api/stream":
            name = qs.get("server", [""])[0]
            if name not in SERVERS:
                return self._json({"error": "unknown server"}, 404)
            return self._stream(SERVERS[name])

        self._json({"error": "not found"}, 404)

    def _stream(self, srv: Managed) -> None:
        """Server-Sent Events: one console line per event."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = srv.subscribe()
        try:
            while True:
                try:
                    line = q.get(timeout=15)
                    payload = json.dumps({"line": line})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")   # keep proxies honest
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            srv.unsubscribe(q)

    def do_POST(self) -> None:                                  # noqa: N802
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)

        path = url.path
        name = body.get("server", "")

        if path == "/api/start-all":
            threading.Thread(target=start_all, daemon=True).start()
            return self._json({"ok": True})
        if path == "/api/stop-all":
            threading.Thread(target=stop_all, daemon=True).start()
            return self._json({"ok": True})

        if path in ("/api/start", "/api/stop", "/api/restart"):
            if name not in SERVERS:
                return self._json({"error": "unknown server"}, 404)
            srv = SERVERS[name]
            if path == "/api/start":
                srv.retries = 0
                ok, msg = srv.start()
            elif path == "/api/stop":
                ok, msg = srv.stop()
            else:
                srv.stop()
                srv.retries = 0
                ok, msg = srv.start()
            return self._json({"ok": ok, "message": msg})

        if path == "/api/config-save":
            res = write_config(body.get("name", ""), body.get("content", ""),
                               body.get("mtime"))
            return self._json(res, 200 if "error" not in res else 400)

        if path == "/api/command":
            target = body.get("server") or ""
            srv = SERVERS.get(target) if target in SERVERS else None
            if srv is None or not srv.wants_console:
                srv = next((s for s in SERVERS.values() if s.wants_console), None)
            if srv is None:
                return self._json({"error": "no console-capable server"}, 400)
            ok, msg = srv.send(body.get("command", ""))
            return self._json({"ok": ok, "message": msg})

        self._json({"error": "not found"}, 404)


def resolve_browser(spec: str) -> list[str] | None:
    """Turn the configured browser into an argv prefix, or None if unusable.

    Accepts a bare command on PATH, an absolute path, or either with extra
    arguments. Returning None is the caller's cue to fall back to the default.
    """
    spec = (spec or "").strip()
    if not spec:
        return None
    try:
        parts = shlex.split(spec)
    except ValueError:                       # unbalanced quotes in the TOML
        print(f"browser setting {spec!r} is not parseable — using system default")
        return None
    if not parts:
        return None
    exe = shutil.which(parts[0])
    if not exe:
        cand = os.path.expanduser(parts[0])
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            exe = cand
    if not exe:
        print(f"browser {parts[0]!r} not found — using system default")
        return None
    return [exe, *parts[1:]]


def open_panel(url: str) -> None:
    """Open the panel, preferring the configured browser."""
    argv = resolve_browser(APP.get("browser", ""))
    if argv:
        try:
            subprocess.Popen([*argv, url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            print(f"opened in {os.path.basename(argv[0])}")
            return
        except OSError as exc:
            print(f"could not launch {argv[0]}: {exc} — using system default")

    # webbrowser honours $BROWSER and then the xdg default.
    try:
        if webbrowser.open(url):
            print("opened in the system default browser")
            return
    except Exception:                                      # noqa: BLE001
        pass
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        print("opened via xdg-open")
    else:
        print(f"open {url} manually")


def preflight() -> list[str]:
    """Problems worth naming at startup rather than discovering in the UI."""
    warn = []
    if psutil is None:
        warn.append("psutil is not installed: servers started outside the "
                    "panel will not be detected, and their uptime is unknown")
    if not SERVERS:
        warn.append("no [servers.*] sections are enabled — nothing to manage")
    for srv in SERVERS.values():
        if not srv.managed:
            continue
        if not srv.exe:
            warn.append(f"{srv.name}: no executable configured")
        elif not os.path.isfile(srv.exe):
            warn.append(f"{srv.name}: executable not found: {srv.exe}")
    if DB.get("enabled") and not os.path.isfile(DB_CLIENT):
        warn.append(f"database client not found: {DB_CLIENT or '(unset)'}")
    if DB.get("enabled") and DB.get("password") and not DB.get("defaults_file"):
        warn.append("[database].password sits in this config file; a "
                    "--defaults-file with 0600 permissions is safer")
    for key in ASSET_KEYS:
        if BRAND.get(key) and not (asset_path(key) or Path()).is_file():
            warn.append(f"branding.{key} not found: {BRAND[key]}")
    for name, raw in CONFIGS.get("files", {}).items():
        if not Path(os.path.expanduser(raw)).is_file():
            warn.append(f"editable config {name!r} not found: {raw}")
    return warn


def main() -> int:
    if OPTS.print_config:
        json.dump(CFG, sys.stdout, indent=2, default=str)
        print()
        return 0

    # Unbuffered-ish: started from a .desktop entry or with output redirected,
    # startup warnings must not sit in a buffer until the process exits.
    sys.stdout.reconfigure(line_buffering=True)

    host = APP.get("bind", "127.0.0.1")
    port = int(APP.get("port", 8090))
    if not (BASE / "static" / "index.html").is_file():
        print("static/index.html missing", file=sys.stderr)
        return 1

    # The panel has no authentication: anyone who can reach it can edit server
    # configs and type arbitrary commands into the world console. Binding it
    # off-loopback has to be a deliberate act, not a typo.
    if host not in LOOPBACK and not APP.get("allow_remote_without_auth", False):
        print(f"refusing to bind {host}: this panel has NO authentication.\n"
              f"Reach it remotely over an SSH tunnel:\n"
              f"  ssh -N -L {port}:127.0.0.1:{port} user@host\n"
              f"To bind anyway, set allow_remote_without_auth = true "
              f"under [app].", file=sys.stderr)
        return 2

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        # Almost always another panel already on this port. Say which port and
        # what to do, rather than printing a socket traceback.
        print(f"cannot listen on {host}:{port}: {exc}\n"
              f"Another wowadmin (or something else) may already be there. "
              f"Change [app].port, or stop the other one.", file=sys.stderr)
        return 2
    httpd.daemon_threads = True
    print(f"wowadmin on http://{host}:{port}  (Ctrl-C to stop the panel)")
    print(f"config: {CONFIG_PATH}")
    if SERVERS:
        print(f"managing: {', '.join(SERVERS[n].display for n in ORDER)}")
    for line in preflight():
        print(f"  ! {line}")

    if APP.get("autostart", False):
        threading.Thread(target=start_all, daemon=True).start()

    if not OPTS.no_browser:
        # Slight delay so the listener is accepting before the tab opens.
        threading.Timer(0.8, open_panel, args=(f"http://{host}:{port}",)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down panel (servers are left running)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
