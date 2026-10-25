# Sourced by every Slurm script: loads deploy/s2db.env and defines the submit
# helpers, so resource settings live in one place and job chains resubmit the
# scripts on disk (edits take effect at the next rotation).

DEPLOY_DIR=${DEPLOY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
ENV_FILE=${S2DB_ENV:-$DEPLOY_DIR/s2db.env}
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (copy s2db.env.example)" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
export S2DB_ENV=$ENV_FILE DEPLOY_DIR

S2DB=$S2DB_HOME/.venv/bin/s2db
SLURM_DIR=$DEPLOY_DIR/slurm
AUTO=$S2DB_STATE_DIR/auto
mkdir -p "$S2DB_STATE_DIR" "$S2DB_LOG_DIR" "$AUTO"

_common_args() {
  local a=()
  [ -n "${SLURM_ACCOUNT:-}" ] && a+=(-A "$SLURM_ACCOUNT")
  [ -n "${SLURM_PARTITION:-}" ] && a+=(-p "$SLURM_PARTITION")
  a+=(--export=ALL,S2DB_ENV="$S2DB_ENV",DEPLOY_DIR="$DEPLOY_DIR")
  printf '%s\n' "${a[@]}"
}

# sbatch_job NAME SCRIPT [extra sbatch args...] -- [script args...]
sbatch_job() {
  local name=$1 script=$2; shift 2
  local sargs=() jargs=() seen=0
  for x in "$@"; do
    if [ "$x" = "--" ]; then seen=1; continue; fi
    if [ $seen -eq 0 ]; then sargs+=("$x"); else jargs+=("$x"); fi
  done
  mapfile -t common < <(_common_args)
  sbatch --parsable -J "$name" -o "$S2DB_LOG_DIR/%x-%j.log" "${common[@]}" "${sargs[@]}" \
    "$SLURM_DIR/$script" "${jargs[@]}"
}

submit_serve() { sbatch_job s2db-serve serve.sbatch -d singleton -t "$CHAIN_TIME" -c "$SERVE_CPUS" --mem="$SERVE_MEM"; }
submit_watch() { sbatch_job s2db-watch watch.sbatch -d singleton -t "$CHAIN_TIME" -c 1 --mem=1G; }

# queue the next link of a chain unless one is already waiting
chain_successor() {
  if squeue -h -u "$USER" -n "$SLURM_JOB_NAME" -t PENDING -o %i | grep -q .; then
    echo "[chain] successor already pending"
  else
    "$1" >&2 && echo "[chain $(date '+%F %T')] successor queued" || echo "[chain] successor submit failed"
  fi
}

log_status() { echo "[$(date '+%F %T')] $*" | tee -a "$AUTO/status.log"; }
