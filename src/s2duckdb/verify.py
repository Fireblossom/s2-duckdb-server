"""Sanity-check a freshly built cache against the one currently online.

    s2db verify NEW_CACHE_DIR CURRENT_CACHE_DIR

Fails (exit 1) if a table is missing, empty, or shrank by more than
--max-shrink. S2 deletes make small shrinks legitimate; a big drop means a
broken build. Used by deploy/slurm/swap.sbatch before going live.
"""

from pathlib import Path

import duckdb

TABLES = {
    "citations": ["citation_details", "citing_aggregated"],
    "papers": ["papers_optimized"],
}


def counts(path: Path, tables):
    con = duckdb.connect(str(path), read_only=True)
    try:
        return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        con.close()


def main(a) -> int:
    new_dir, cur_dir = Path(a.new_cache_dir), Path(a.current_cache_dir)
    ok = True
    for kind, db in (("citations", a.citations_db), ("papers", a.papers_db)):
        new_db, cur_db = new_dir / db, cur_dir / db
        if not new_db.exists():
            print(f"FAIL {db}: not built")
            ok = False
            continue
        try:
            new = counts(new_db, TABLES[kind])
        except Exception as e:  # missing table, corrupt file, ...
            print(f"FAIL {db}: {e}")
            ok = False
            continue
        cur = counts(cur_db, TABLES[kind]) if cur_db.exists() else {}
        for t, n in new.items():
            old = cur.get(t)
            change = "" if not old else f" ({(n - old) / old:+.2%} vs {old:,})"
            bad = n == 0 or bool(old and n < old * (1 - a.max_shrink))
            print(f"{'FAIL' if bad else 'ok  '} {db}:{t} {n:,} rows{change}")
            ok = ok and not bad
    return 0 if ok else 1
