#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
BACKEND_RELOAD="${BACKEND_RELOAD:-0}"

BACKEND_LOG="${BACKEND_LOG:-/tmp/gasum_backend.log}"
FRONTEND_LOG="${FRONTEND_LOG:-/tmp/gasum_frontend.log}"
BACKEND_PID_FILE="${BACKEND_PID_FILE:-/tmp/gasum_backend.pid}"
FRONTEND_PID_FILE="${FRONTEND_PID_FILE:-/tmp/gasum_frontend.pid}"
BACKEND_SCREEN_SESSION="${BACKEND_SCREEN_SESSION:-gasum_backend}"
FRONTEND_SCREEN_SESSION="${FRONTEND_SCREEN_SESSION:-gasum_frontend}"

is_pid_running() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

has_screen() {
  command -v screen >/dev/null 2>&1
}

screen_session_exists() {
  local session="$1"
  screen -ls 2>/dev/null | grep -Eq "[0-9]+\.${session}[[:space:]]"
}

port_listener_pid() {
  local port="$1"
  lsof -tiTCP:"$port" -sTCP:LISTEN -n -P 2>/dev/null | head -n 1 || true
}

wait_for_port() {
  local port="$1"
  local name="$2"
  local timeout_secs="${3:-30}"
  local elapsed=0

  while [ "$elapsed" -lt "$timeout_secs" ]; do
    if lsof -iTCP:"$port" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
      echo "$name OK: listening on $port"
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  echo "$name FAIL: not listening on $port after ${timeout_secs}s"
  return 1
}

start_backend() {
  local listener
  listener="$(port_listener_pid "$BACKEND_PORT")"
  if [ -n "$listener" ]; then
    echo "Backend FAIL: port $BACKEND_PORT already in use (pid=$listener) but not managed by $BACKEND_PID_FILE."
    echo "Run ./scripts/app_down.sh first, then retry ./scripts/app_up.sh."
    return 1
  fi

  if [ ! -x ".venv/bin/python" ]; then
    echo "Backend FAIL: .venv/bin/python not found. Create venv and install requirements first."
    return 1
  fi

  local reload_flag=""
  if [ "$BACKEND_RELOAD" = "1" ]; then
    reload_flag="--reload"
  fi

  : > "$BACKEND_LOG"
  if has_screen; then
    if screen_session_exists "$BACKEND_SCREEN_SESSION"; then
      screen -S "$BACKEND_SCREEN_SESSION" -X quit || true
      sleep 1
    fi
    screen -S "$BACKEND_SCREEN_SESSION" -dm \
      zsh -lc "cd '$ROOT_DIR' && .venv/bin/python -m uvicorn backend.main:app $reload_flag --host '$BACKEND_HOST' --port '$BACKEND_PORT' >> '$BACKEND_LOG' 2>&1"
    echo "Backend starting (screen=$BACKEND_SCREEN_SESSION, reload=$BACKEND_RELOAD) log=$BACKEND_LOG"
  else
    nohup .venv/bin/python -m uvicorn backend.main:app $reload_flag --host "$BACKEND_HOST" --port "$BACKEND_PORT" \
      > "$BACKEND_LOG" 2>&1 &
    echo "Backend starting (pid=$!, reload=$BACKEND_RELOAD) log=$BACKEND_LOG"
  fi

  if ! wait_for_port "$BACKEND_PORT" "Backend"; then
    rm -f "$BACKEND_PID_FILE"
    return 1
  fi
  port_listener_pid "$BACKEND_PORT" > "$BACKEND_PID_FILE"
}

start_frontend() {
  local listener
  listener="$(port_listener_pid "$FRONTEND_PORT")"
  if [ -n "$listener" ]; then
    echo "Frontend FAIL: port $FRONTEND_PORT already in use (pid=$listener) but not managed by $FRONTEND_PID_FILE."
    echo "Run ./scripts/app_down.sh first, then retry ./scripts/app_up.sh."
    return 1
  fi

  if [ ! -d "frontend/node_modules" ]; then
    echo "Frontend FAIL: frontend/node_modules missing. Run 'cd frontend && npm install' first."
    return 1
  fi

  : > "$FRONTEND_LOG"
  if has_screen; then
    if screen_session_exists "$FRONTEND_SCREEN_SESSION"; then
      screen -S "$FRONTEND_SCREEN_SESSION" -X quit || true
      sleep 1
    fi
    screen -S "$FRONTEND_SCREEN_SESSION" -dm \
      zsh -lc "cd '$ROOT_DIR/frontend' && npm run dev -- --host '$FRONTEND_HOST' --port '$FRONTEND_PORT' >> '$FRONTEND_LOG' 2>&1"
    echo "Frontend starting (screen=$FRONTEND_SCREEN_SESSION) log=$FRONTEND_LOG"
  else
    nohup zsh -lc "cd '$ROOT_DIR/frontend' && npm run dev -- --host '$FRONTEND_HOST' --port '$FRONTEND_PORT'" \
      > "$FRONTEND_LOG" 2>&1 &
    echo "Frontend starting (pid=$!) log=$FRONTEND_LOG"
  fi

  if ! wait_for_port "$FRONTEND_PORT" "Frontend"; then
    rm -f "$FRONTEND_PID_FILE"
    return 1
  fi
  port_listener_pid "$FRONTEND_PORT" > "$FRONTEND_PID_FILE"
}

echo "Starting GASUM backend + frontend in detached mode..."
start_backend
start_frontend

echo ""
echo "Ready:"
echo "  Backend:  http://localhost:${BACKEND_PORT}/docs"
echo "  Frontend: http://localhost:${FRONTEND_PORT}"
echo ""
echo "Use ./scripts/app_status.sh to inspect runtime state."
