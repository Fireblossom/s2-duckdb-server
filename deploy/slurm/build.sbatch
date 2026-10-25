#!/bin/bash
# Build one cache into a staging dir (submitted by check.sh).
#   build.sbatch papers|citations STAGING_DIR
# Corrupt or truncated .gz files are moved to $S2DB_RAW_DIR/../raw_corrupt/
# (never deleted) so one bad download cannot block every future rebuild.
set -uo pipefail
source "${DEPLOY_DIR:?}/slurm/common.sh"
KIND=${1:?papers|citations}
STG=${2:?staging dir}
case $KIND in
  papers)    SUBS="papers abstracts paper-ids papers-deletes abstracts-deletes paper-ids-deletes" ;;
  citations) SUBS="citations citations-deletes" ;;
  *) echo "unknown kind $KIND"; exit 2 ;;
esac
echo "[$(date '+%F %T')] $KIND on $(hostname), $SLURM_CPUS_PER_TASK cpus, ${SLURM_MEM_PER_NODE} MB -> $STG"

QUAR=$(dirname "$S2DB_RAW_DIR")/raw_corrupt
for s in $SUBS; do
  [ -d "$S2DB_RAW_DIR/$s" ] || continue
  bad=$(find "$S2DB_RAW_DIR/$s" -name "*.gz" -print0 \
        | xargs -0 -P "$SLURM_CPUS_PER_TASK" -I{} sh -c 'gzip -t "$1" 2>/dev/null || echo "$1"' _ {})
  for f in $bad; do
    mkdir -p "$QUAR/$s"; mv "$f" "$QUAR/$s/"; echo "  quarantined $s/$(basename "$f")"
  done
done

"$S2DB" build --"$KIND" --convert --force \
  --raw-dir "$S2DB_RAW_DIR" --data-dir "$S2DB_DATA_DIR" --cache-dir "$STG" \
  --workers $(( SLURM_CPUS_PER_TASK > 8 ? SLURM_CPUS_PER_TASK - 4 : SLURM_CPUS_PER_TASK ))
rc=$?
echo "[$(date '+%F %T')] exit code $rc"
exit $rc
