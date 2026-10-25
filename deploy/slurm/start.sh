#!/bin/bash
# Start (or restart) the long-running jobs from a login node.
#
#   deploy/slurm/start.sh            serve + watch
#   deploy/slurm/start.sh serve      only the server
#   deploy/slurm/start.sh watch      only the rebuild watcher
#   deploy/slurm/start.sh stop       cancel all s2db jobs (running builds included)
#
# Restarting cancels the old chain first; the server is down for about a minute.
set -uo pipefail
DEPLOY_DIR=$(cd "$(dirname "$0")/.." && pwd)
source "$DEPLOY_DIR/slurm/common.sh"

cancel() { ids=$(squeue -h -u "$USER" -o "%i %j" | awk -v n="$1" '$2 == n {print $1}'); [ -n "$ids" ] && scancel $ids; }

case "${1:-all}" in
  stop)  for n in s2db-serve s2db-watch s2db-auto-papers s2db-auto-citations s2db-auto-swap; do cancel $n; done
         echo "stopped" ;;
  serve) cancel s2db-serve; sleep 3; echo "serve job $(submit_serve)" ;;
  watch) cancel s2db-watch; sleep 3; echo "watch job $(submit_watch)" ;;
  all)   cancel s2db-serve; cancel s2db-watch; sleep 3
         echo "serve job $(submit_serve)"; echo "watch job $(submit_watch)" ;;
  *)     echo "usage: $0 [all|serve|watch|stop]"; exit 2 ;;
esac
echo "logs: $S2DB_LOG_DIR   status: $AUTO/status.log   api key: $S2DB_STATE_DIR/api_key"
