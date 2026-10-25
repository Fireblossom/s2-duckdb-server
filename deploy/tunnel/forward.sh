#!/bin/bash
# Consumer side: keep localhost:LOCAL_PORT forwarded to RELAY_PORT on the relay,
# where serve.sbatch's reverse tunnel ends. Reconnects forever.
#
#   forward.sh user@relay.example.org [RELAY_PORT=18300] [LOCAL_PORT=8300] [KEY=~/.ssh/s2db_tunnel]
#
# Start it from cron through watchdog.sh so it also survives reboots.
set -u
RELAY=${1:?usage: forward.sh user@relay [relay_port] [local_port] [key]}
RELAY_PORT=${2:-18300}
LOCAL_PORT=${3:-8300}
KEY=${4:-$HOME/.ssh/s2db_tunnel}
while true; do
  ssh -4 -N -i "$KEY" -o IdentitiesOnly=yes -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new \
      -L "127.0.0.1:$LOCAL_PORT:localhost:$RELAY_PORT" "$RELAY"
  echo "[forward down $(date '+%F %T')] retry 5s"
  sleep 5
done
