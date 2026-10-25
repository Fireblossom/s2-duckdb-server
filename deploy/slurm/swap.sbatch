#!/bin/bash
# Verify the staging caches, put them live, make the server reload.
#   swap.sbatch TAG        (submitted by check.sh with afterok on both builds)
# The previous version is kept in $S2DB_CACHE_DIR/prev_auto/ (one version only;
# anything else in the cache dir is left alone).
set -uo pipefail
source "${DEPLOY_DIR:?}/slurm/common.sh"
TAG=${1:?TAG}
STG=$S2DB_CACHE_DIR/staging_$TAG
DBS="${S2DB_CITATIONS_DB:-citations.duckdb} ${S2DB_PAPERS_DB:-papers.duckdb}"
fail() { log_status "FAILED swap $TAG: $*"; date +%s > "$AUTO/last_failure"; exit 1; }

log_status "swap $TAG: verifying $STG"
"$S2DB" verify "$STG" "$S2DB_CACHE_DIR" | tee -a "$AUTO/status.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || fail "verification failed, new cache left in $STG"

# same filesystem -> atomic renames; the running server keeps the old inodes
mkdir -p "$S2DB_CACHE_DIR/prev_auto"
for f in $DBS; do rm -f "$S2DB_CACHE_DIR/prev_auto/$f"; done
for f in $DBS; do
  [ -e "$S2DB_CACHE_DIR/$f" ] && { mv "$S2DB_CACHE_DIR/$f" "$S2DB_CACHE_DIR/prev_auto/$f" || fail "mv old $f"; }
done
for f in $DBS; do mv "$STG/$f" "$S2DB_CACHE_DIR/$f" || fail "mv new $f (old copy in prev_auto/)"; done
rm -rf "$STG"
cp "$AUTO/candidate_$TAG.txt" "$AUTO/built_manifest.txt"
rm -f "$AUTO/last_failure" "$AUTO"/candidate_*.txt
log_status "swap $TAG: new cache online, previous version in prev_auto/"

before=$(cat "$S2DB_STATE_DIR/serving" 2>/dev/null)
echo reload > "$S2DB_STATE_DIR/serve.ctrl"
for _ in $(seq 60); do
  sleep 10
  now=$(cat "$S2DB_STATE_DIR/serving" 2>/dev/null)
  if [ -n "$now" ] && [ "$now" != "$before" ]; then
    log_status "swap $TAG: server reloaded ($now)"
    exit 0
  fi
done
log_status "WARNING swap $TAG: server did not confirm reload within 10 min (is s2db-serve running?)"
