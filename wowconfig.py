"""Configuration helpers shared by wowadmin's front ends.

The web panel (app.py) and the terminal launcher (tmux.py) read the same realm
TOML through these functions, so one file describes a realm for both.
Stdlib only.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:                           # pragma: no cover
    # tomllib arrived in 3.11, and the bare ImportError names only the module.
    raise SystemExit(
        f"wowadmin needs Python 3.11 or newer; this is "
        f"{sys.version_info.major}.{sys.version_info.minor}.")

BASE = Path(__file__).resolve().parent
VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
WINDOWS = os.name == "nt"


def is_executable(path: str) -> bool:
    """Windows has no execute bit; being a file is as much as can be asked."""
    if not os.path.isfile(path):
        return False
    return True if WINDOWS else os.access(path, os.X_OK)


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
    # Always defined, so SQL written to exclude bots stays valid on a realm
    # that has none: "NOT LIKE \'\'" excludes nothing, which is exactly right,
    # and "LIKE \'\'" matches nothing, which is also exactly right.
    vars.setdefault("bot_pattern", "")
    expanded = {k: expand(v, vars) for k, v in cfg.items() if k != "vars"}
    expanded["vars"] = vars
    # The raw text is kept because one question can only be asked of it: which
    # queries refer to ${bot_pattern}. After expansion that is unknowable.
    expanded["_raw"] = cfg
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


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0
