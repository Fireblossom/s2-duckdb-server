#!/bin/bash
# Long-running s2db server. Submit with deploy/slurm/start.sh (resources come
# from s2db.env), not with sbatch directly.
#
#   - binds 127.0.0.1:$S2DB_PORT on the compute node
#   - optional self-healing reverse tunnel to $RELAY:$RELAY_PORT
#   - supervisor: restarts the server if it dies; $S2DB_STATE_DIR/serve.ctrl
#       run    (default)
#       hold   stop the server, keep the allocation
#       reload restart on the current cache files (swap.sbatch does this after a rebuild)
#   - after every healthy start: writes $S2DB_STATE_DIR/serving and runs `s2db warmup`
#   - queues its successor at start (-d singleton), so the service survives the time limit
set -uo pipefail
source "${DEPLOY_DIR:?run via deploy/slurm/start.sh}/slurm/common.sh"

CTRL=$S2DB_STATE_DIR/serve.ctrl
KEYFILE=$S2DB_STATE_DIR/api_key
DB=$S2DB_CACHE_DIR/${S2DB_CITATIONS_DB:-citations.duckdb}
PAPERS=$S2DB_CACHE_DIR/${S2DB_PAPERS_DB:-papers.duckdb}

echo run > "$CTRL"
[ -s "$KEYFILE" ] || (umask 077; openssl rand -hex 24 > "$KEYFILE")
export S2DB_API_KEY=$(<"$KEYFILE")
echo "node $(hostname) cpus=$SLURM_CPUS_PER_TASK mem=${SLURM_MEM_PER_NODE}MB port=$S2DB_PORT relay=${RELAY:-none}"

REVTUN=""
if [ -n "${RELAY:-}" ]; then
  (
    while true; do
      ssh -N -i "$TUNNEL_KEY" -o IdentitiesOnly=yes -o ExitOnForwardFailure=yes \
          -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new \
          -R "$RELAY_PORT:localhost:$S2DB_PORT" "$RELAY"
      echo "[revtun down $(date '+%F %T')] retry 5s"
      sleep 5
    done
  ) &
  REVTUN=$!
fi

chain_successor submit_serve

SERVER="" WARM=""
start_server() {
  if [ ! -e "$DB" ] || [ ! -e "$PAPERS" ]; then
    # first deployment: nothing built yet; check again every loop, log once
    [ -z "${WAITING:-}" ] && echo "[supervisor $(date '+%F %T')] no cache yet in $S2DB_CACHE_DIR, waiting for the first build"
    WAITING=1
    return
  fi
  WAITING=""
  echo "[supervisor $(date '+%F %T')] launch"
  "$S2DB" serve --database "$DB" --attach "papers=$PAPERS" --read-only --no-external-access \
      --host 127.0.0.1 --port "$S2DB_PORT" \
      --threads "$SLURM_CPUS_PER_TASK" --memory-limit "$(( SLURM_MEM_PER_NODE * 8 / 10 ))MB" &
  SERVER=$!
  for _ in $(seq 60); do
    sleep 2
    kill -0 "$SERVER" 2>/dev/null || return
    if curl -sf -m 5 "http://127.0.0.1:$S2DB_PORT/health" >/dev/null; then
      echo "$(date '+%F %T') $(hostname) inode=$(stat -L -c %i "$DB")" > "$S2DB_STATE_DIR/serving"
      echo "[supervisor $(date '+%F %T')] healthy"
      "$S2DB" warmup --url "http://127.0.0.1:$S2DB_PORT" --top "${WARMUP_TOP:-500}" &
      WARM=$!
      return
    fi
  done
}
stop_server() {
  [ -n "$WARM" ] && kill "$WARM" 2>/dev/null; WARM=""
  [ -n "$SERVER" ] && kill "$SERVER" 2>/dev/null && wait "$SERVER" 2>/dev/null
  SERVER=""
}
trap '[ -n "$REVTUN" ] && kill $REVTUN 2>/dev/null; stop_server' EXIT

while true; do
  case "$(cat "$CTRL" 2>/dev/null)" in
    hold)   stop_server ;;
    reload) echo "[supervisor $(date '+%F %T')] reload requested"
            stop_server; echo run > "$CTRL"; start_server ;;
    *)      if [ -z "$SERVER" ] || ! kill -0 "$SERVER" 2>/dev/null; then
              [ -n "$SERVER" ] && { wait "$SERVER"; echo "[supervisor $(date '+%F %T')] server exited rc=$?, relaunch in 5s"; SERVER=""; sleep 5; }
              start_server
            fi ;;
  esac
  sleep 10
done
