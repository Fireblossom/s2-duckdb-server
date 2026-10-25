#!/bin/bash
# Tiny long-running job that runs check.sh every $CHECK_INTERVAL seconds
# (for clusters where scrontab/cron are not available). Chains itself like serve.sbatch.
set -uo pipefail
source "${DEPLOY_DIR:?run via deploy/slurm/start.sh}/slurm/common.sh"
chain_successor submit_watch
while true; do
  echo "[$(date '+%F %T')] $("$SLURM_DIR/check.sh" 2>&1 | tail -1)"
  sleep "${CHECK_INTERVAL:-900}"
done
