#!/bin/bash
# One rebuild check (watch.sbatch runs it every $CHECK_INTERVAL seconds):
# if $S2DB_RAW_DIR has files the live cache was not built from, and the sync is
# finished, submit papers + citations builds into a staging dir and a swap job.
#
#   check.sh            normal check
#   check.sh --dry-run  print what would happen, submit nothing
#
# "Sync finished" = a release marker (.s2db/done/<release>, written by
# `s2db download`, and copied last by deploy/sync/rclone-push.sh) is newer than
# every new file, or else no new file arrived for $QUIET_MIN minutes.
# Arrival time is ctime: rclone/rsync preserve mtime.
set -uo pipefail
source "${DEPLOY_DIR:-$(cd "$(dirname "$0")/.." && pwd)}/slurm/common.sh"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

# ---- 1. a round in progress: clean up after failed builds, otherwise wait ----
# Pending reasons that never resolve on their own: a failed dependency, or a
# request the partition/QOS/account can never satisfy (fix s2db.env, then it retries).
STUCK='DependencyNeverSatisfied|PartitionTimeLimit|PartitionNodeLimit|QOSMax[A-Za-z]*PerJob|AssocMax[A-Za-z]*PerJob|InvalidAccount|InvalidQOS|BadConstraints'
jobs=$(squeue -h -u "$USER" -o "%i %j %T %r" | awk '$2 ~ /^s2db-auto-/')
if [ -n "$jobs" ]; then
  if echo "$jobs" | grep -qE "$STUCK"; then
    ids=$(echo "$jobs" | awk '{print $1}' | tr '\n' ' ')
    log_status "FAILED: job(s) can never run: $(echo "$jobs" | grep -oE "$STUCK" | sort -u | tr '\n' ' ')- cancelling $ids (see $S2DB_LOG_DIR)"
    [ $DRY -eq 0 ] && { scancel $ids; date +%s > "$AUTO/last_failure"; }
  else
    echo "rebuild in progress: $(echo "$jobs" | awk '{print $2"="$3}' | tr '\n' ' ')"
  fi
  exit 0
fi

# ---- 2. new files? ----
find "$S2DB_RAW_DIR" -path "$S2DB_RAW_DIR/.s2db" -prune -o -name "*.gz" -printf "%P\n" \
  | sort > "$AUTO/current.txt"
MANIFEST=$AUTO/built_manifest.txt   # written by swap.sbatch after each successful round
if [ ! -f "$MANIFEST" ]; then
  echo "no built_manifest.txt yet: treating everything in raw/ as new"
  MANIFEST=/dev/null
fi
comm -13 "$MANIFEST" "$AUTO/current.txt" > "$AUTO/new.txt"
n=$(wc -l < "$AUTO/new.txt")
[ "$n" -eq 0 ] && { echo "no new files"; exit 0; }

newest=$(cd "$S2DB_RAW_DIR" && tr '\n' '\0' < "$AUTO/new.txt" | xargs -0 stat -c %Z | sort -n | tail -1)
age_min=$(( ($(date +%s) - newest) / 60 ))
marker=$(find "$S2DB_RAW_DIR/.s2db/done" -type f -printf "%C@ %f\n" 2>/dev/null | sort -n | tail -1)
marker_t=${marker%%.*}; marker_t=${marker_t:-0}
if [ "$marker_t" -ge "$newest" ]; then
  reason="release marker ${marker#* }"
elif [ "$age_min" -ge "$QUIET_MIN" ]; then
  reason="no new file for ${age_min} min"
else
  echo "$n new files, last ${age_min} min ago, no release marker yet; waiting (quiet period ${QUIET_MIN} min)"
  exit 0
fi

if [ -f "$AUTO/last_failure" ]; then
  since_h=$(( ($(date +%s) - $(cat "$AUTO/last_failure")) / 3600 ))
  if [ "$since_h" -lt "$RETRY_AFTER_FAIL_H" ]; then
    echo "last rebuild failed ${since_h}h ago; retry after ${RETRY_AFTER_FAIL_H}h"
    exit 0
  fi
fi

# ---- 3. remove staging dirs of failed rounds (kept RETRY_AFTER_FAIL_H for debugging) ----
for d in "$S2DB_CACHE_DIR"/staging_*; do
  [ -d "$d" ] || continue
  log_status "removing stale $d"
  [ $DRY -eq 0 ] && rm -rf "$d"
done

# ---- 4. submit builds + swap ----
TAG=$(date +%Y%m%d_%H%M)
STG=$S2DB_CACHE_DIR/staging_$TAG
summary=$(awk -F/ '{n[$1]++} END{for(k in n) printf "%s=%d ", k, n[k]}' "$AUTO/new.txt")
log_status "new data: $n files (${summary% }), ready: $reason -> rebuild $TAG"
[ $DRY -eq 1 ] && { echo "(dry-run) would build into $STG"; exit 0; }

cp "$AUTO/current.txt" "$AUTO/candidate_$TAG.txt"
build_args=(-c "$BUILD_CPUS" -t "$BUILD_TIME")
j1=$(sbatch_job s2db-auto-papers build.sbatch "${build_args[@]}" --mem="$BUILD_MEM_PAPERS" -- papers "$STG") \
  || { log_status "ERROR: submitting papers build failed"; exit 1; }
j2=$(sbatch_job s2db-auto-citations build.sbatch "${build_args[@]}" --mem="$BUILD_MEM_CITATIONS" -- citations "$STG") \
  || { scancel "$j1"; log_status "ERROR: submitting citations build failed"; exit 1; }
j3=$(sbatch_job s2db-auto-swap swap.sbatch -c 4 --mem=32G -t "${SWAP_TIME:-00:30:00}" -d "afterok:$j1:$j2" -- "$TAG") \
  || { scancel "$j1" "$j2"; log_status "ERROR: submitting swap failed"; exit 1; }
log_status "submitted papers=$j1 citations=$j2 swap=$j3"
