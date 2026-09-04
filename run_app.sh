#!/bin/bash
# Activates the venv and launches the Streamlit grading dashboard.
# Usage: ./run_app.sh
set -e
cd "$(dirname "$0")"
source .venv/bin/activate
streamlit run app.py --server.address=127.0.0.1
