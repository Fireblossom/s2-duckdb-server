#!/bin/bash
# Restart forward.sh if it is gone or its local port stops accepting TCP.
# Deliberately ignores HTTP errors: a busy or restarting server is not a
# tunnel fault, and restarting the tunnel would only add a refused window.
#
# crontab -e:
#   */2 * * * * /path/to/deploy/tunnel/watchdog.sh user@relay 18300 8300
#   @reboot     /path/to/deploy/tunnel/watchdog.sh user@relay 18300 8300
PATH=/usr/sbin:/usr/bin:/sbin:/bin
RELAY=${1:?usage: watchdog.sh user@relay [relay_port] [local_port] [key]}
RELAY_PORT=${2:-18300}
LOCAL_PORT=${3:-8300}
KEY=${4:-$HOME/.ssh/s2db_tunnel}
HERE=$(cd "$(dirname "$0")" && pwd)
LOG=$HOME/.s2db-forward-$LOCAL_PORT.log
PAT="forward.sh $RELAY $RELAY_PORT $LOCAL_PORT"

ok() {
  pgrep -f "$PAT" >/dev/null 2>&1 || return 1
  timeout 5 bash -c "cat </dev/null >/dev/tcp/127.0.0.1/$LOCAL_PORT" 2>/dev/null
}
ok && exit 0
sleep 5
ok && exit 0

echo "$(date '+%F %T') tunnel down -> restarting" >> "$LOG"
pkill -9 -f "$PAT" 2>/dev/null
pkill -9 -f "127.0.0.1:$LOCAL_PORT:localhost:$RELAY_PORT" 2>/dev/null
sleep 1
setsid -f bash "$HERE/forward.sh" "$RELAY" "$RELAY_PORT" "$LOCAL_PORT" "$KEY" >>"$LOG" 2>&1 </dev/null
