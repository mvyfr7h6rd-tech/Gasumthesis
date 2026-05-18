#!/usr/bin/env bash
set -euo pipefail

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
BACKEND_LOG="${BACKEND_LOG:-/tmp/gasum_backend.log}"
FRONTEND_LOG="${FRONTEND_LOG:-/tmp/gasum_frontend.log}"
BACKEND_PID_FILE="${BACKEND_PID_FILE:-/tmp/gasum_backend.pid}"
FRONTEND_PID_FILE="${FRONTEND_PID_FILE:-/tmp/gasum_frontend.pid}"
BACKEND_SCREEN_SESSION="${BACKEND_SCREEN_SESSION:-gasum_backend}"
FRONTEND_SCREEN_SESSION="${FRONTEND_SCREEN_SESSION:-gasum_frontend}"

print_proc_state() {
  local name="$1"
  local port="$2"
  local pid_file="$3"
  local pid="-"

  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file" 2>/dev/null || echo "-")"
  fi

  if lsof -iTCP:"$port" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
    echo "$name: UP (port $port listening, pid_file=$pid)"
    lsof -iTCP:"$port" -sTCP:LISTEN -n -P
  else
    echo "$name: DOWN (port $port not listening, pid_file=$pid)"
  fi
  echo ""
}

echo "GASUM app status"
echo "==============="

if command -v screen >/dev/null 2>&1; then
  echo "Screen sessions:"
  if screen -ls 2>/dev/null | grep -Eq "[0-9]+\.${BACKEND_SCREEN_SESSION}[[:space:]]"; then
    echo "Backend screen: UP ($BACKEND_SCREEN_SESSION)"
  else
    echo "Backend screen: DOWN ($BACKEND_SCREEN_SESSION)"
  fi
  if screen -ls 2>/dev/null | grep -Eq "[0-9]+\.${FRONTEND_SCREEN_SESSION}[[:space:]]"; then
    echo "Frontend screen: UP ($FRONTEND_SCREEN_SESSION)"
  else
    echo "Frontend screen: DOWN ($FRONTEND_SCREEN_SESSION)"
  fi
  echo ""
fi

print_proc_state "Backend" "$BACKEND_PORT" "$BACKEND_PID_FILE"
print_proc_state "Frontend" "$FRONTEND_PORT" "$FRONTEND_PID_FILE"

if [ -f "$BACKEND_LOG" ]; then
  echo "Last backend log lines ($BACKEND_LOG):"
  tail -n 8 "$BACKEND_LOG" || true
  echo ""
fi

if [ -f "$FRONTEND_LOG" ]; then
  echo "Last frontend log lines ($FRONTEND_LOG):"
  tail -n 8 "$FRONTEND_LOG" || true
fi
