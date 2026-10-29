#!/bin/sh
# Independent environment only; no sudo, robotd configuration, service or motors.
set -eu
cd "$(dirname "$0")"
python3 -c 'import sys; assert (3,11) <= sys.version_info[:2] <= (3,13), "Use CPython 3.11/3.12/3.13 with these pinned wheels"'
[ ! -f .venv/Scripts/python.exe ] || { echo 'Windows .venv found; do not overwrite it from Linux. Use a separate checkout.' >&2; exit 1; }
python3 -m venv .venv
.venv/bin/python --version
# Fail clearly on missing platform wheels, rather than compiling on the board.
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m pip freeze > .venv/installed-requirements.txt
.venv/bin/python prepare.py "$@"
.venv/bin/python recognize.py --check
