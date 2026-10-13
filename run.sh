#!/bin/bash
# SuperCam relay lifecycle:  ./run.sh start|stop|status
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDF="$DIR/run.pid"
PY="${PYTHON:-python3}"
PORT="${PORT:-8099}"
case "$1" in
  start)
    [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null && sleep 1
    cd "$DIR" || exit 1
    nohup "$PY" relay.py > /tmp/supercam-relay.log 2>&1 &
    echo $! > "$PIDF"; echo "started pid $(cat "$PIDF") on :$PORT" ;;
  stop)
    if [ -f "$PIDF" ]; then
      P=$(cat "$PIDF"); kill "$P" 2>/dev/null; sleep 1
      pkill -P "$P" 2>/dev/null; kill -9 "$P" 2>/dev/null; rm -f "$PIDF"
    fi
    echo stopped ;;
  status)
    curl -s --max-time 5 "http://127.0.0.1:$PORT/health" || echo "not running"; echo ;;
  *) echo "usage: $0 start|stop|status" ;;
esac
