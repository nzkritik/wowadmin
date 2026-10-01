#!/bin/bash
# Run the realm in a tmux session: a control panel window plus one window per
# server, for a machine you reach over SSH. Same config as the web panel.
#
#   ./run-tmux.sh [--config FILE]                  create the session if needed, attach
#   ./run-tmux.sh [--config FILE] start|stop|restart [SERVER]
#   ./run-tmux.sh [--config FILE] status|down
#
# Detaching (Ctrl-b d, or q in the control window) leaves the realm running.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
exec python3 wowtmux.py "$@"
