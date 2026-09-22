#!/usr/bin/env bash
# Starts the Instagram analyzer locally.
# First run creates a virtual environment and installs dependencies;
# every run after that just activates it and launches the server.

set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Setting up virtual environment (first run only)..."
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
else
  source .venv/bin/activate
fi

echo "Starting server at http://127.0.0.1:8000 ..."
uvicorn app:app --reload
