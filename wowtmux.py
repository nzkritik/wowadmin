#!/usr/bin/env python3
"""wowadmin in tmux: the realm in a terminal session, for a server you reach over SSH.

One tmux session per realm. Window "control" holds a full-screen control panel
(start, stop, restart, live system and process stats, players online); every
server in the config gets a window of its own, where it runs directly on the
pane's terminal. Its console is the real one: switch to the window and type.
Detach and the realm keeps running; attach again from any SSH login.

It reads the same realm TOML as the web panel (app.py), so one file describes a
realm for both. Use one front end or the other for a running realm: each
recognises the other's servers as "running outside" and leaves them be.

    ./run-tmux.sh                      create the session if needed, and attach
    ./run-tmux.sh start [SERVER]       start everything in order, or one server
    ./run-tmux.sh stop [SERVER]        stop everything in reverse order, or one
    ./run-tmux.sh restart [SERVER]
    ./run-tmux.sh status
    ./run-tmux.sh down                 stop everything and close the session

Linux only (it reads /proc). Needs tmux 3.0+ and Python 3.11+, nothing else.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from wowconfig import BASE, find_config, is_executable, load_config, port_open, resolve_exe

PAGE = os.sysconf("SC_PAGE_SIZE")
TICKS = os.sysconf("SC_CLK_TCK")


# --- tmux -------------------------------------------------------------------

def tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def tmux_out(*args: str) -> str:
    res = tmux(*args)
    return res.stdout.rstrip("\n") if res.returncode == 0 else ""


# --- /proc ------------------------------------------------------------------

def boot_time() -> float:
    with open("/proc/stat") as fh:
        for line in fh:
            if line.startswith("btime "):
                return float(line.split()[1])
    return 0.0


BOOT = boot_time()


def proc_times(pid: int) -> tuple[int, float, int] | None:
    """(cpu ticks used, start time as epoch seconds, resident bytes) or None."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            raw = fh.read()
        with open(f"/proc/{pid}/statm") as fh:
            rss = int(fh.read().split()[1]) * PAGE
    except (OSError, ValueError, IndexError):
        return None
    # The command name sits in parentheses and may itself contain spaces.
    fields = raw[raw.rindex(")") + 2:].split()
    ticks = int(fields[11]) + int(fields[12])           # utime + stime
    started = BOOT + int(fields[19]) / TICKS
    return ticks, started, rss


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def find_processes(exe: str, args: list[str] | None = None) -> list[int]:
    """PIDs of this user's processes running `exe` (with exactly `args`, if given).

    A script's /proc/<pid>/exe is its interpreter, so the argument list is
    checked too: that is how a server started from a wrapper is still found.
    """
    if not exe:
        return []
    target = os.path.realpath(exe)
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0")[:-1]]
            if not argv:
                continue
            if os.path.realpath(f"/proc/{pid}/exe") == target:
                at = 0
            else:
                at = next((i for i, a in enumerate(argv[:2]) if os.path.realpath(a) == target), -1)
                if at < 0:
                    continue
        except OSError:
            continue
        if args is None or argv[at + 1:] == args:
            found.append(pid)
    return found


# --- the realm --------------------------------------------------------------

class Realm:
    """The servers of one config, each run in its own window of one tmux session."""

    def __init__(self, config: Path, log=print):
        self.config_path = config
        self.cfg = load_config(config)
        self.app = self.cfg.get("app", {})
        self.opts = self.cfg.get("tmux", {})
        self.log = log
        self.session = self.opts.get("session") or f"wowadmin-{config.stem}"
        self.servers = {name: spec for name, spec in self.cfg.get("servers", {}).items()
                        if spec.get("enabled", True)}
        self.order = list(self.servers)
        self.cancel = threading.Event()
        raw = self.app.get("log_dir")
        base = (Path(os.path.expanduser(os.path.expandvars(raw))) if raw else
                Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()
                / "wowadmin" / config.stem)
        self.log_dir = base

    # -- helpers -----------------------------------------------------------

    def target(self, name: str) -> str:
        return f"{self.session}:{name}"

    def display(self, name: str) -> str:
        return self.servers[name].get("display_name", name)

    def exe(self, name: str) -> str:
        return resolve_exe(self.servers[name].get("executable", ""))

    def log_path(self, name: str) -> Path:
        return self.log_dir / f"tmux-{name}.log"

    def session_exists(self) -> bool:
        return tmux("has-session", "-t", f"={self.session}").returncode == 0

    def get_opt(self, name: str, key: str) -> str:
        return tmux_out("show-options", "-wqv", "-t", self.target(name), f"@wa_{key}")

    def set_opt(self, name: str, key: str, value: str) -> None:
        tmux("set-option", "-wq", "-t", self.target(name), f"@wa_{key}", value)

    # -- session -----------------------------------------------------------

    def create_session(self, autostart: bool = False) -> None:
        """The session, its control window and one idle window per server."""
        me = [sys.executable, str(Path(__file__).resolve()), "--config", str(self.config_path), "tui"]
        if autostart:
            me.append("--autostart")
        res = tmux("new-session", "-d", "-s", self.session, "-n", "control", "-x", "160", "-y", "48",
                   "-c", str(BASE), *me)
        if res.returncode != 0:
            raise SystemExit(f"tmux could not create session {self.session}: "
                             f"{res.stderr.strip() or 'exit ' + str(res.returncode)}")
        # Scrollback for the server windows, which inherit it when created.
        tmux("set-option", "-t", self.session, "history-limit",
             str(int(self.opts.get("history_limit", 50000))))
        tmux("set-option", "-t", self.session, "status-left",
             f"[{self.cfg.get('realm', {}).get('name', self.session)}] ")
        tmux("set-option", "-t", self.session, "status-left-length", "30")
        # The control panel survives a crash long enough for the error to be read.
        tmux("set-option", "-w", "-t", f"{self.session}:control", "remain-on-exit", "on")
        for name in self.order:
            note = (f"{self.display(name)} is stopped. Start it from the control window: "
                    f"switch with Ctrl-b then w, or run: {Path(__file__).parent / 'run-tmux.sh'} start {name}")
            tmux("new-window", "-d", "-t", f"{self.session}:", "-n", name,
                 "sh", "-c", f"printf '%s\\n' {shlex.quote(note)}")
            tmux("set-option", "-w", "-t", self.target(name), "remain-on-exit", "on")
            self.set_opt(name, "kind", "idle")
        tmux("select-window", "-t", f"{self.session}:control")

    def attach(self) -> None:
        if os.environ.get("TMUX"):
            os.execvp("tmux", ["tmux", "switch-client", "-t", self.session])
        os.execvp("tmux", ["tmux", "attach-session", "-t", self.session])

    # -- status ------------------------------------------------------------

    def pane(self, name: str) -> dict:
        fmt = "#{pane_dead}|#{pane_pid}|#{pane_dead_status}"
        out = tmux_out("display-message", "-p", "-t", self.target(name), fmt)
        if not out:
            return {"exists": False}
        dead, pid, status = (out.split("|") + ["", "", ""])[:3]
        return {"exists": True, "dead": dead == "1", "pid": int(pid or 0),
                "exit": status or None}

    def pane_pids(self) -> set[int]:
        """PIDs of the live processes in this session's panes."""
        out = tmux_out("list-panes", "-s", "-t", f"={self.session}", "-F", "#{pane_dead} #{pane_pid}")
        return {int(pid) for dead, pid in (l.split() for l in out.splitlines() if l) if dead == "0"}

    def external_pid(self, name: str) -> int:
        """A copy of this server running somewhere other than this session's panes."""
        ours = self.pane_pids() if self.session_exists() else set()
        exe = self.exe(name)
        # Two servers on one executable are told apart by their arguments.
        shared = sum(1 for n in self.order if self.exe(n) == exe) > 1
        args = [str(a) for a in self.servers[name].get("args", [])] if shared else None
        for pid in find_processes(exe, args):
            if pid not in ours:
                return pid
        return 0

    def is_ready(self, name: str) -> bool:
        """Port open, or the ready line in the pane's scrollback, per the config."""
        if self.get_opt(name, "ready") == "1":
            return True
        spec = self.servers[name]
        port, pattern = spec.get("ready_port"), spec.get("ready_pattern")
        ok = False
        if port and port_open(int(port)):
            ok = True
        elif pattern:
            text = tmux_out("capture-pane", "-p", "-J", "-S", "-", "-t", self.target(name))
            ok = re.search(pattern, text) is not None
        elif not port:
            ok = True
        if ok:
            self.set_opt(name, "ready", "1")
        return ok

    def status(self, name: str) -> dict:
        """state: stopped | starting | running | stopping | exited | outside | missing."""
        p = self.pane(name)
        if not p["exists"]:
            return {"state": "missing", "pid": 0}
        kind, state = self.get_opt(name, "kind"), self.get_opt(name, "state")
        if kind == "server" and not p["dead"]:
            if state == "stopping":
                return {"state": "stopping", "pid": p["pid"]}
            return {"state": "running" if self.is_ready(name) else "starting", "pid": p["pid"]}
        other = self.external_pid(name)
        if other:
            return {"state": "outside", "pid": other}
        if kind == "server" and state not in ("stopped", "stopping"):
            return {"state": "exited", "pid": 0, "exit": p.get("exit")}
        return {"state": "stopped", "pid": 0}

    def running(self, name: str) -> bool:
        return self.status(name)["state"] in ("starting", "running", "stopping", "outside")

    # -- start -------------------------------------------------------------

    def _spawn(self, name: str) -> None:
        spec = self.servers[name]
        exe = self.exe(name)
        cwd = os.path.expanduser(spec.get("working_dir", "")) or os.path.dirname(exe)
        argv = [exe, *[str(a) for a in spec.get("args", [])]]
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = self.log_path(name)
        if not spec.get("log_append"):
            log.write_text("")
        self.set_opt(name, "kind", "server")
        self.set_opt(name, "state", "starting")
        self.set_opt(name, "ready", "0")
        tmux("pipe-pane", "-t", self.target(name))                  # close any old pipe
        tmux("clear-history", "-t", self.target(name))              # readiness reads this run only
        res = tmux("respawn-pane", "-k", "-t", self.target(name), "-c", cwd, *argv)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "tmux respawn-pane failed")
        tmux("pipe-pane", "-t", self.target(name), f"exec cat >> {shlex.quote(str(log))}")

    def start(self, name: str) -> bool:
        """Start one server and wait until it is ready, retrying as configured."""
        spec = self.servers[name]
        st = self.status(name)
        if st["state"] in ("starting", "running", "stopping"):
            self.log(f"{self.display(name)} is already {st['state']}")
            return st["state"] != "stopping"
        if st["state"] == "outside":
            self.log(f"{self.display(name)} is already running outside this session "
                     f"(pid {st['pid']}); leaving it alone")
            return True
        exe = self.exe(name)
        if not is_executable(exe):
            self.log(f"{self.display(name)}: not executable: {exe}")
            return False

        retries = int(spec.get("max_retries", 0) or 0)
        delay = float(spec.get("retry_delay", 3.0) or 3.0)
        timeout = float(spec.get("ready_timeout", 60.0))
        for attempt in range(retries + 1):
            if self.cancel.is_set():
                return False
            if attempt:
                self.log(f"{self.display(name)} exited during start; retry {attempt} of {retries} "
                         f"in {delay:g}s")
                if self.cancel.wait(delay):
                    return False
            self.log(f"starting {self.display(name)}")
            try:
                self._spawn(name)
            except RuntimeError as exc:
                self.log(f"{self.display(name)}: {exc}")
                return False
            deadline = time.time() + timeout
            while time.time() < deadline and not self.cancel.is_set():
                st = self.status(name)
                if st["state"] == "running":
                    self.set_opt(name, "state", "running")
                    self.log(f"{self.display(name)} is ready (pid {st['pid']})")
                    return True
                if st["state"] not in ("starting",):
                    break
                time.sleep(0.5)
            else:
                if self.cancel.is_set():
                    return False
                self.log(f"{self.display(name)} did not report ready within {timeout:g}s; "
                         f"leaving it running - check its window")
                return False
        st = self.status(name)
        self.log(f"{self.display(name)} did not start ({st['state']}"
                 f"{', exit ' + st['exit'] if st.get('exit') else ''}) - see its window")
        return False

    def start_all(self) -> bool:
        for i, name in enumerate(self.order):
            if self.servers[name].get("manage", True) is False:
                continue
            if not self.start(name):
                later = ", ".join(self.display(n) for n in self.order[i + 1:])
                if later and not self.cancel.is_set():
                    self.log(f"not starting {later}: {self.display(name)} is not up")
                return False
        return True

    # -- stop --------------------------------------------------------------

    def _signal(self, pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except OSError:
            pass

    def stop(self, name: str) -> bool:
        spec = self.servers[name]
        st = self.status(name)
        if st["state"] in ("stopped", "exited", "missing"):
            if st["state"] == "exited":
                self.set_opt(name, "state", "stopped")
            self.log(f"{self.display(name)} is not running")
            return True
        pid = st["pid"]
        timeout = float(self.app.get("shutdown_timeout", 300))
        delay = int(self.app.get("shutdown_delay", 1))
        outside = st["state"] == "outside"
        self.log(f"stopping {self.display(name)}{' (started outside this session)' if outside else ''}")
        if not outside:
            self.set_opt(name, "state", "stopping")

        if not outside and spec.get("console") and st["state"] == "running":
            # The server's own graceful path, typed into its console. While it
            # is still loading it may not read the console yet; SIGTERM, which
            # the cores also handle by saving and exiting, is the safer lever.
            command = spec.get("stop_console_command", "server shutdown {delay}").format(delay=delay)
            tmux("send-keys", "-t", self.target(name), "-l", command)
            tmux("send-keys", "-t", self.target(name), "Enter")
            self.log(f"sent '{command}' to the {self.display(name)} console")
        elif spec.get("stop_command"):
            cmd = [resolve_exe(spec["stop_command"][0]), *spec["stop_command"][1:]]
            subprocess.run(cmd, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self._signal(pid, signal.SIGTERM)

        def wait(seconds: float) -> bool:
            end = time.time() + seconds
            while time.time() < end:
                if not pid_alive(pid):
                    return True
                time.sleep(0.5)
            return not pid_alive(pid)

        if not wait(timeout):
            self.log(f"{self.display(name)} still running after {timeout:g}s - sending SIGTERM")
            self._signal(pid, signal.SIGTERM)
            if not wait(60 if spec.get("console") else 30):
                self.log(f"{self.display(name)} ignored SIGTERM - killing it")
                self._signal(pid, signal.SIGKILL)
                wait(5)
        if not outside:
            self.set_opt(name, "state", "stopped")
        self.log(f"{self.display(name)} stopped")
        return True

    def stop_all(self) -> None:
        for name in reversed(self.order):
            if self.servers[name].get("manage", True) is False:
                continue
            if self.running(name):
                self.stop(name)

    def restart(self, name: str | None) -> bool:
        if name:
            self.stop(name)
            return self.start(name)
        self.stop_all()
        return self.start_all()

    # -- figures -------------------------------------------------------------

    def players(self) -> str:
        """The [database].count_query answer, or '' when there is none to give."""
        db = self.cfg.get("database", {})
        sql = db.get("count_query")
        client = resolve_exe(db.get("client", ""))
        if not (db.get("enabled") and sql and client and os.path.isfile(client)):
            return ""
        cmd = [client]
        env = None
        if db.get("defaults_file"):
            cmd.append(f"--defaults-file={os.path.expanduser(db['defaults_file'])}")
        else:
            if db.get("socket"):
                cmd += ["--protocol=SOCKET", f"--socket={db['socket']}"]
            else:
                cmd.append(f"--host={db.get('host', '127.0.0.1')}")
                if db.get("port"):
                    cmd.append(f"--port={int(db['port'])}")
            if db.get("user"):
                cmd.append(f"--user={db['user']}")
            if db.get("password"):                     # never in argv
                env = {**os.environ, "MYSQL_PWD": str(db["password"])}
        if db.get("database"):
            cmd.append(f"--database={db['database']}")
        cmd += ["--batch", "--raw", "--skip-column-names", "-e", sql]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return out.stdout.strip().split("\n")[0].split("\t")[0] if out.returncode == 0 else ""

    def disk_path(self) -> str:
        if self.opts.get("disk"):
            return os.path.expanduser(self.opts["disk"])
        for name in self.order:
            wd = self.servers[name].get("working_dir")
            if wd and os.path.isdir(os.path.expanduser(wd)):
                return os.path.expanduser(wd)
        return "/"


# --- system figures ---------------------------------------------------------

class Sampler:
    """CPU percentages need two readings; this keeps the previous one."""

    def __init__(self):
        self.prev_cpu: tuple[int, int] | None = None
        self.prev_proc: dict[int, tuple[int, float]] = {}

    def system(self, disk: str) -> dict:
        with open("/proc/stat") as fh:
            parts = [int(x) for x in fh.readline().split()[1:]]
        idle, total = parts[3] + parts[4], sum(parts[:8])
        cpu = None
        if self.prev_cpu:
            d_total = total - self.prev_cpu[1]
            cpu = 100.0 * (1 - (idle - self.prev_cpu[0]) / d_total) if d_total else 0.0
        self.prev_cpu = (idle, total)
        mem = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, val = line.split(":", 1)
                mem[key] = int(val.split()[0]) * 1024
        with open("/proc/loadavg") as fh:
            load = fh.read().split()[:3]
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
        try:
            st = os.statvfs(disk)
            d_total, d_free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        except OSError:
            d_total = d_free = 0
        return {"cpu": cpu, "cores": os.cpu_count() or 1, "load": load, "uptime": up,
                "mem_total": mem.get("MemTotal", 0), "mem_avail": mem.get("MemAvailable", 0),
                "swap_total": mem.get("SwapTotal", 0), "swap_free": mem.get("SwapFree", 0),
                "disk": disk, "disk_total": d_total, "disk_free": d_free}

    def process(self, pid: int) -> dict:
        info = proc_times(pid) if pid else None
        if not info:
            self.prev_proc.pop(pid, None)
            return {}
        ticks, started, rss = info
        now = time.time()
        cpu = None
        if pid in self.prev_proc:
            p_ticks, p_time = self.prev_proc[pid]
            if now > p_time:
                cpu = 100.0 * (ticks - p_ticks) / TICKS / (now - p_time)
        self.prev_proc[pid] = (ticks, now)
        return {"cpu": cpu, "rss": rss, "uptime": now - started}


def human_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit in ("B", "K") else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def human_time(s: float) -> str:
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d{h:02d}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


# --- the control window -----------------------------------------------------

def run_tui(realm: Realm, autostart: bool) -> None:
    import curses
    import queue
    from collections import deque

    messages: deque[str] = deque(maxlen=200)
    lock = threading.Lock()

    def log(msg: str) -> None:
        with lock:
            messages.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    realm.log = log
    jobs: queue.Queue = queue.Queue()
    busy = {"what": ""}
    figures = {"players": "", "players_at": 0.0}

    def worker() -> None:
        while True:
            label, fn = jobs.get()
            busy["what"] = label
            realm.cancel.clear()
            try:
                fn()
            except Exception as exc:                       # noqa: BLE001
                log(f"{label} failed: {exc}")
            busy["what"] = ""

    def players_loop() -> None:
        interval = float(realm.cfg.get("database", {}).get("interval", 15.0))
        while True:
            figures["players"] = realm.players()
            figures["players_at"] = time.time()
            time.sleep(interval)

    threading.Thread(target=worker, daemon=True).start()
    if realm.cfg.get("database", {}).get("count_query"):
        threading.Thread(target=players_loop, daemon=True).start()

    def submit(label: str, fn, preempt: bool = False) -> None:
        if busy["what"] and preempt:
            log(f"cancelling: {busy['what']}")
            realm.cancel.set()
        elif busy["what"] or not jobs.empty():
            log(f"queued: {label}")
        jobs.put((label, fn))

    if autostart:
        submit("start all", realm.start_all)

    sampler = Sampler()
    # Plain ASCII on screen: a login without a UTF-8 locale cannot draw more,
    # and Python's own output encoding does not reveal what tmux can show.
    arrow, dot = "> ", "-"
    title = realm.cfg.get("branding", {}).get("title") or "wowadmin"
    realm_name = realm.cfg.get("realm", {}).get("name", "")

    def main(scr) -> None:
        curses.curs_set(0)
        scr.timeout(1000)
        curses.use_default_colors()
        colours = {}
        if curses.has_colors():
            for i, (key, fg) in enumerate((("green", curses.COLOR_GREEN), ("yellow", curses.COLOR_YELLOW),
                                           ("red", curses.COLOR_RED), ("cyan", curses.COLOR_CYAN),
                                           ("blue", curses.COLOR_BLUE)), start=1):
                curses.init_pair(i, fg, -1)
                colours[key] = curses.color_pair(i)
        state_style = {"running": colours.get("green", 0) | curses.A_BOLD,
                       "starting": colours.get("yellow", 0), "stopping": colours.get("yellow", 0),
                       "exited": colours.get("red", 0) | curses.A_BOLD, "outside": colours.get("cyan", 0),
                       "stopped": curses.A_DIM, "missing": colours.get("red", 0)}
        sel = 0
        confirm: dict | None = None
        sys_fig = sampler.system(realm.disk_path())
        last_sys = 0.0
        statuses: dict[str, dict] = {}
        procs: dict[str, dict] = {}

        def put(y: int, x: int, text: str, attr: int = 0) -> None:
            h, w = scr.getmaxyx()
            if 0 <= y < h and x < w:
                try:
                    scr.addnstr(y, x, text, max(0, w - x - 1), attr)
                except curses.error:
                    pass

        while True:
            now = time.time()
            if now - last_sys >= 1.0:
                sys_fig = sampler.system(realm.disk_path())
                for name in realm.order:
                    statuses[name] = realm.status(name)
                    procs[name] = sampler.process(statuses[name].get("pid", 0))
                last_sys = now

            scr.erase()
            h, w = scr.getmaxyx()
            head = f" {title}" + (f"  {dot}  realm {realm_name}" if realm_name else "")
            put(0, 0, head.ljust(w), curses.A_REVERSE | curses.A_BOLD)
            put(0, max(0, w - 10), time.strftime("%H:%M:%S"), curses.A_REVERSE | curses.A_BOLD)

            y = 2
            put(y, 2, f"{'':2}{'#':<3}{'SERVER':<18}{'STATE':<11}{'PID':>8}  {'UPTIME':>8}  {'CPU':>6}  {'MEMORY':>8}",
                curses.A_BOLD)
            y += 1
            for i, name in enumerate(realm.order):
                st = statuses.get(name, {"state": "?"})
                pr = procs.get(name, {})
                marker = arrow if i == sel else "  "
                state = st["state"]
                label = "outside" if state == "outside" else state
                if state == "exited" and st.get("exit"):
                    label = f"exited {st['exit']}"
                cpu = f"{pr['cpu']:.0f}%" if pr.get("cpu") is not None else "-"
                put(y, 2, marker, curses.A_BOLD)
                put(y, 4, f"{i + 1:<3}{realm.display(name)[:17]:<18}", curses.A_BOLD if i == sel else 0)
                put(y, 25, f"{label:<11}", state_style.get(state, 0))
                put(y, 36, f"{st.get('pid') or '-':>8}  {human_time(pr['uptime']) if pr else '-':>8}  "
                           f"{cpu:>6}  {human_bytes(pr['rss']) if pr else '-':>8}")
                y += 1

            y += 1
            s = sys_fig
            mem_used = s["mem_total"] - s["mem_avail"]
            swap_used = s["swap_total"] - s["swap_free"]
            disk_used = s["disk_total"] - s["disk_free"]
            cpu = f"{s['cpu']:.0f}%" if s["cpu"] is not None else "-"
            put(y, 2, "SYSTEM", curses.A_BOLD)
            put(y, 12, f"CPU {cpu} of {s['cores']} cores   load {' '.join(s['load'])}   "
                       f"up {human_time(s['uptime'])}")
            y += 1
            put(y, 12, f"RAM {human_bytes(mem_used)} / {human_bytes(s['mem_total'])}"
                       f"   swap {human_bytes(swap_used)} / {human_bytes(s['swap_total'])}")
            y += 1
            if s["disk_total"]:
                pct = 100 * disk_used / s["disk_total"]
                put(y, 12, f"disk {human_bytes(disk_used)} / {human_bytes(s['disk_total'])} ({pct:.0f}%)  "
                           f"{s['disk']}")
                y += 1
            if realm.cfg.get("database", {}).get("count_query"):
                put(y, 2, "PLAYERS", curses.A_BOLD)
                shown = figures["players"] or ("..." if not figures["players_at"] else "unavailable")
                put(y, 12, f"{shown} online")
                y += 1

            y += 1
            keys = [("s/x/r", "start/stop/restart selected"), ("S/X/R", "all servers"),
                    ("Enter", "open its window"), ("up/down 1-9", "select"),
                    ("q", "detach")]
            x = 2
            for k, d in keys:
                put(y, x, k, colours.get("blue", 0) | curses.A_BOLD)
                put(y, x + len(k) + 1, d)
                x += len(k) + len(d) + 4
                if x > w - 20:
                    y += 1
                    x = 2
            y += 1
            put(y, 2, "In a server window: Ctrl-b w lists windows, Ctrl-b d detaches. "
                      "Servers keep running when you detach.", curses.A_DIM)
            y += 2

            if confirm:
                put(y, 2, f" {confirm['text']} [y/N] ", colours.get("yellow", 0) | curses.A_REVERSE | curses.A_BOLD)
            elif busy["what"]:
                put(y, 2, f"working: {busy['what']}", colours.get("yellow", 0) | curses.A_BOLD)
            y += 2

            put(y, 2, "ACTIVITY", curses.A_BOLD)
            y += 1
            with lock:
                lines = list(messages)[-(max(0, h - y - 1)):]
            for line in lines:
                put(y, 4, line)
                y += 1
            scr.refresh()

            try:
                ch = scr.get_wch()
            except curses.error:
                continue
            if ch == curses.KEY_RESIZE:
                continue
            if confirm:
                if ch in ("y", "Y"):
                    submit(confirm["label"], confirm["fn"], confirm.get("preempt", False))
                else:
                    log("cancelled")
                confirm = None
                continue
            name = realm.order[sel] if realm.order else None
            if ch in (curses.KEY_UP, "k"):
                sel = max(0, sel - 1)
            elif ch in (curses.KEY_DOWN, "j"):
                sel = min(len(realm.order) - 1, sel + 1)
            elif isinstance(ch, str) and ch.isdigit() and 0 < int(ch) <= len(realm.order):
                sel = int(ch) - 1
            elif ch in ("\n", "\r", curses.KEY_ENTER, "g") and name:
                tmux("select-window", "-t", realm.target(name))
            elif ch == "s" and name:
                submit(f"start {realm.display(name)}", lambda n=name: realm.start(n))
            elif ch == "S":
                submit("start all", realm.start_all)
            elif ch == "x" and name:
                confirm = {"text": f"Stop {realm.display(name)}?", "label": f"stop {realm.display(name)}",
                           "fn": lambda n=name: realm.stop(n), "preempt": True}
            elif ch == "X":
                confirm = {"text": "Stop ALL servers?", "label": "stop all", "fn": realm.stop_all,
                           "preempt": True}
            elif ch == "r" and name:
                confirm = {"text": f"Restart {realm.display(name)}?", "label": f"restart {realm.display(name)}",
                           "fn": lambda n=name: realm.restart(n), "preempt": True}
            elif ch == "R":
                confirm = {"text": "Restart ALL servers?", "label": "restart all",
                           "fn": lambda: realm.restart(None), "preempt": True}
            elif ch == "q":
                tmux("detach-client", "-s", realm.session)

    log(f"control panel ready - session {realm.session}, config {realm.config_path}")
    curses.wrapper(main)


# --- command line -----------------------------------------------------------

def print_status(realm: Realm) -> None:
    sampler = Sampler()
    print(f"session {realm.session}: {'up' if realm.session_exists() else 'not running'}")
    for name in realm.order:
        st = realm.status(name) if realm.session_exists() else {"state": "no session", "pid": 0}
        if st["state"] == "no session" and realm.external_pid(name):
            st = {"state": "outside", "pid": realm.external_pid(name)}
        pr = sampler.process(st.get("pid", 0))
        extra = f"  up {human_time(pr['uptime'])}  {human_bytes(pr['rss'])}" if pr else ""
        print(f"  {realm.display(name):<18} {st['state']:<11} {st.get('pid') or '':>8}{extra}")


def main() -> int:
    ap = argparse.ArgumentParser(description="wowadmin realm control in tmux")
    ap.add_argument("--config", "-c", help="path to the realm's wowadmin TOML")
    ap.add_argument("command", nargs="?", default="up",
                    choices=["up", "start", "stop", "restart", "status", "down", "tui"])
    ap.add_argument("server", nargs="?", help="one server by its [servers.NAME] key")
    ap.add_argument("--autostart", action="store_true", help="tui: start all on launch")
    ap.add_argument("--start", action="store_true", help="up: start all servers when creating the session")
    ap.add_argument("--no-attach", action="store_true", help="up: create the session but do not attach")
    opts = ap.parse_args()

    if not sys.platform.startswith("linux"):
        raise SystemExit("wowtmux reads /proc and runs on Linux only")
    if subprocess.run(["which", "tmux"], capture_output=True).returncode != 0:
        raise SystemExit("tmux is not installed (Debian: sudo apt install tmux)")

    realm = Realm(find_config(opts.config))
    if opts.server and opts.server not in realm.servers:
        raise SystemExit(f"no server '{opts.server}' in the config; "
                         f"these exist: {', '.join(realm.order)}")

    if opts.command == "tui":
        run_tui(realm, opts.autostart)
        return 0
    if opts.command == "status":
        print_status(realm)
        return 0

    if opts.command == "up":
        if not realm.session_exists():
            realm.create_session(autostart=opts.start or bool(realm.app.get("autostart")))
            print(f"created tmux session {realm.session}")
        if opts.no_attach:
            return 0
        realm.attach()
        return 0

    if not realm.session_exists():
        if opts.command in ("stop", "down"):
            print(f"no session {realm.session}")
            return 0
        realm.create_session()
    ok = True
    if opts.command == "start":
        ok = realm.start(opts.server) if opts.server else realm.start_all()
    elif opts.command == "stop":
        realm.stop(opts.server) if opts.server else realm.stop_all()
    elif opts.command == "restart":
        ok = realm.restart(opts.server)
    elif opts.command == "down":
        realm.stop_all()
        tmux("kill-session", "-t", f"={realm.session}")
        print(f"closed tmux session {realm.session}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
