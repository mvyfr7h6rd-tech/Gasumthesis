#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
BACKEND_PID_FILE="${BACKEND_PID_FILE:-/tmp/gasum_backend.pid}"
FRONTEND_PID_FILE="${FRONTEND_PID_FILE:-/tmp/gasum_frontend.pid}"
BACKEND_SCREEN_SESSION="${BACKEND_SCREEN_SESSION:-gasum_backend}"
FRONTEND_SCREEN_SESSION="${FRONTEND_SCREEN_SESSION:-gasum_frontend}"

stop_screen_session() {
  local name="$1"
  local session="$2"
  if command -v screen >/dev/null 2>&1 && screen -ls 2>/dev/null | grep -Eq "[0-9]+\.${session}[[:space:]]"; then
    screen -S "$session" -X quit || true
    echo "$name: stopped screen session $session"
  fi
}

stop_pid_file_process() {
  local name="$1"
  local pid_file="$2"

  if [ ! -f "$pid_file" ]; then
    echo "$name: no pid file ($pid_file)"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [ -z "$pid" ]; then
    rm -f "$pid_file"
    echo "$name: empty pid file removed"
    return 0
  fi

  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "$name: stopped pid $pid"
  else
    echo "$name: pid $pid already stopped"
  fi

  rm -f "$pid_file"
}

stop_port_processes() {
  local name="$1"
  local port="$2"
  local pids
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN -n -P 2>/dev/null || true)"

  if [ -z "$pids" ]; then
    echo "$name: no listeners on port $port"
    return 0
  fi

  echo "$name: stopping listeners on port $port -> $pids"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
}

echo "Stopping GASUM backend + frontend..."
stop_screen_session "Backend" "$BACKEND_SCREEN_SESSION"
stop_screen_session "Frontend" "$FRONTEND_SCREEN_SESSION"
stop_pid_file_process "Backend" "$BACKEND_PID_FILE"
stop_pid_file_process "Frontend" "$FRONTEND_PID_FILE"

# Cleanup any stale listeners that were not started with pid files.
stop_port_processes "Backend" "$BACKEND_PORT"
stop_port_processes "Frontend" "$FRONTEND_PORT"

echo "Done."
