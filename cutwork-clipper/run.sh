#!/usr/bin/env bash
# Mac / Linux: double-click or run  ./run.sh
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
python app.py
