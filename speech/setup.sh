#!/bin/sh
# Independent environment only; no sudo, robotd configuration, service or motors.
set -eu
cd "$(dirname "$0")"
[ ! -f .venv/Scripts/python.exe ] || { echo 'Windows .venv found; do not overwrite it from Linux. Use a separate checkout.' >&2; exit 1; }
python=${PYTHON:-python3}
if [ -z "${PYTHON:-}" ] && [ -x .venv/bin/python ]; then
    python=.venv/bin/python
elif [ -z "${PYTHON:-}" ]; then
    for candidate in python3 python3.13 python3.12 python3.11; do
        if "$candidate" -c 'import sys; sys.exit(not (3,11) <= sys.version_info[:2] <= (3,13))' 2>/dev/null; then
            python=$candidate
            break
        fi
    done
    if ! "$python" -c 'import sys; sys.exit(not (3,11) <= sys.version_info[:2] <= (3,13))' 2>/dev/null && command -v uv >/dev/null 2>&1; then
        python=$(uv python find --no-project --no-python-downloads 3.12)
    fi
fi
"$python" -c 'import sys; assert (3,11) <= sys.version_info[:2] <= (3,13), "Use CPython 3.11/3.12/3.13; select one with PYTHON=/path/to/python3.12"'
[ -x .venv/bin/python ] || "$python" -m venv .venv
.venv/bin/python -c 'import sys; assert (3,11) <= sys.version_info[:2] <= (3,13), "Existing .venv is unsupported; move it aside before setup"'
.venv/bin/python --version
# Fail clearly on missing platform wheels, rather than compiling on the board.
export PIP_TIMEOUT="${PIP_TIMEOUT:-90}" PIP_RETRIES="${PIP_RETRIES:-8}" PIP_RESUME_RETRIES="${PIP_RESUME_RETRIES:-8}"
if ! .venv/bin/python -c 'import pip,sys; sys.exit(tuple(map(int,pip.__version__.split(".")[:2])) < (25,1))'; then
    .venv/bin/python -m pip install --only-binary=:all: 'pip>=25.1'
fi
.venv/bin/python -m pip install --only-binary=:all: -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m pip freeze > .venv/installed-requirements.txt
.venv/bin/python prepare.py "$@"
.venv/bin/python recognize.py --check
