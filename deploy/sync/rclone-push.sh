#!/bin/bash
# Push a download mirror to one or more rclone remotes, verify, optionally
# free the local copy. Meant as the `s2db download --on-complete` hook when the
# download host is not the build host (small VM with internet -> cluster).
#
#   rclone-push.sh [--delete] REMOTE [REMOTE...]
#   e.g. --on-complete "deploy/sync/rclone-push.sh --delete cluster:/data/s2/raw gdrive:S2AG_archive"
#
# Data files go first, release markers (.s2db/done/*) last, so a marker on the
# remote means its files are complete (check.sh builds as soon as it sees one).
# Remotes with hashes are checked by MD5, others (sftp-only accounts) by size.
# The local mirror's .s2db/state.json is never deleted: it is what lets the
# downloader continue with diffs after local files are freed.
set -uo pipefail
RAW=${S2DB_RAW_DIR:?S2DB_RAW_DIR not set (set by s2db download)}
RCLONE=${RCLONE:-rclone}
LOG=${S2DB_SYNC_LOG:-$RAW/.s2db/sync.log}
DELETE=0; [ "${1:-}" = "--delete" ] && { DELETE=1; shift; }
[ $# -ge 1 ] || { echo "usage: $0 [--delete] REMOTE..." >&2; exit 2; }

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
cd "$RAW" || exit 1
find . -path ./.s2db -prune -o -type f -name "*.gz" -print | sed 's|^\./||' | sort > "$WORK/files"
find .s2db/done -type f 2>/dev/null | sort > "$WORK/markers"
n=$(wc -l < "$WORK/files")
log "release ${S2DB_RELEASE:-?}: pushing $n files to $* (delete=$DELETE)"
[ "$n" -gt 0 ] && "$RCLONE" md5sum . --files-from "$WORK/files" > "$WORK/md5" 2>>"$LOG"

common=(--transfers 4 --retries 5 --low-level-retries 20 --log-file "$LOG" --log-level NOTICE)
failed=0
for R in "$@"; do
  if [ "$n" -gt 0 ]; then
    "$RCLONE" copy . "$R" --files-from "$WORK/files" "${common[@]}" || { log "copy to $R failed"; failed=1; continue; }
    if "$RCLONE" hashsum md5 "$R" --files-from "$WORK/files" >/dev/null 2>&1; then
      "$RCLONE" checksum md5 "$WORK/md5" "$R" --one-way "${common[@]}" || { log "md5 check on $R failed"; failed=1; continue; }
    else
      "$RCLONE" check . "$R" --files-from "$WORK/files" --one-way --size-only "${common[@]}" \
        || { log "size check on $R failed"; failed=1; continue; }
    fi
  fi
  "$RCLONE" copy . "$R" --files-from "$WORK/markers" "${common[@]}" || { log "markers to $R failed"; failed=1; }
done

if [ $failed -ne 0 ]; then log "FAILED: local files kept"; exit 1; fi
if [ $DELETE -eq 1 ] && [ "$n" -gt 0 ]; then
  tr '\n' '\0' < "$WORK/files" | xargs -0 rm -f --
  log "OK: $n files verified on all remotes, local copies deleted"
else
  log "OK: $n files verified on all remotes"
fi
