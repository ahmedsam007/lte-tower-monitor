#!/bin/sh
# Double-click to start Tower Monitor on macOS. Close this window (or press Ctrl+C) to stop it.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Get it from https://www.python.org/downloads/ and try again."
  read -r _; exit 1
fi

# First run only: make a private Python environment and install the packages it needs.
# Later runs skip this, so Tower Monitor still starts when the internet is down.
if [ ! -x .venv/bin/python ]; then
  echo "First start: installing what Tower Monitor needs (one time, needs internet)..."
  python3 -m venv .venv && .venv/bin/python -m pip install --disable-pip-version-check --retries 10 --timeout 30 --only-binary=:all: -r requirements.txt
  if [ $? -ne 0 ]; then
    rm -rf .venv
    echo
    echo "Installing failed. The lines above say why."
    echo "If they mention a connection or timeout, check the internet connection and try again."
    echo "Otherwise open an issue on GitHub with a screenshot of this window."
    read -r _; exit 1
  fi
fi

exec .venv/bin/python server.py "$@"
