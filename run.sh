#!/bin/bash
# Launch the wowadmin control panel.
#
# The panel opens itself in the browser named by [app].browser in the config,
# falling back to the system default. Pass --no-browser to skip that, or
# --config /path/to/wowadmin.toml to drive a particular realm.
# Closing the panel leaves the game servers running.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
exec python3 app.py "$@"
