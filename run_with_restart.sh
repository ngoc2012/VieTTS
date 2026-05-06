#!/bin/bash
# Auto-restart wrapper for VieTTS Flask app on segfault or crash

set +e  # Don't exit on error

RESTART_DELAY=3
MAX_RESTARTS=0  # 0 = infinite

restart_count=0

while true; do
  restart_count=$((restart_count + 1))

  if [ $MAX_RESTARTS -gt 0 ] && [ $restart_count -gt $MAX_RESTARTS ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Max restarts ($MAX_RESTARTS) reached. Exiting."
    exit 1
  fi

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Flask app (attempt $restart_count)..."
  uv run --with flask flask_app.py

  exit_code=$?

  if [ $exit_code -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Flask app exited cleanly (code 0). Exiting."
    exit 0
  fi

  case $exit_code in
    139)
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] Segmentation fault (code 139). Restarting in ${RESTART_DELAY}s..."
      ;;
    *)
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] Exited with code $exit_code. Restarting in ${RESTART_DELAY}s..."
      ;;
  esac

  sleep $RESTART_DELAY
done
