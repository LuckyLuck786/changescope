#!/usr/bin/env bash
set -euo pipefail

echo "=================================================="
echo "  ChangeScope MCP — Web App & MCP Server Launcher"
echo "=================================================="

if [ ! -d ".venv" ]; then
  echo "[1/3] Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "[2/3] Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "[3/3] Starting ChangeScope Web UI & MCP Server on http://localhost:8000 ..."
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
