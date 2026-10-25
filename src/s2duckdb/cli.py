"""s2db: download S2AG, build DuckDB caches, serve and query them.

    s2db download --raw-dir raw [--watch]
    s2db build    --all --raw-dir raw --data-dir data --cache-dir cache
    s2db serve    --database cache/citations.duckdb --attach papers=cache/papers.duckdb --read-only
    s2db query    "SELECT count(*) FROM papers.papers_optimized"
    s2db warmup / s2db verify

Most options also read an S2DB_* environment variable, so one env file can
drive every step (see deploy/s2db.env.example).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from pathlib import Path

from s2duckdb import (
    CITATIONS_DATASETS,
    CITATIONS_DB,
    DEFAULT_DATASETS,
    PAPERS_DATASETS,
    PAPERS_DB,
    __version__,
)


def env(name, default=None):
    return os.environ.get(f"S2DB_{name}", default)


def _deletes(datasets):
    return list(datasets) + [f"{d}-deletes" for d in datasets]


# ---------------------------------------------------------------- build

def cmd_build(a) -> int:
    from s2duckdb.build import CitationCacheBuilder, DuckDBSettings, PapersCacheBuilder

    if not (a.all or a.papers or a.citations):
        print("specify --papers, --citations or --all", file=sys.stderr)
        return 2
    do_papers, do_citations = a.all or a.papers, a.all or a.citations
    settings = DuckDBSettings(
        memory_limit=a.memory_limit, threads=a.threads,
        temp_directory=a.temp_dir, max_temp_directory_size=a.max_temp_size,
    )

    if a.convert:
        from s2duckdb.convert import find_input_files, parallel_convert

        subdirs = (_deletes(PAPERS_DATASETS) if do_papers else []) + (
            _deletes(CITATIONS_DATASETS) if do_citations else [])
        print("\n=== Converting jsonl.gz to parquet ===")
        failed = 0
        for sub in subdirs:
            src = Path(a.raw_dir) / sub
            if not src.is_dir():
                print(f"[WARN] {src} not found, skipping")
                continue
            files = [f for f in find_input_files(str(src)) if not f.name.endswith(".part")]
            print(f"{sub}: {len(files)} files")
            _, errors, _ = parallel_convert(files, Path(a.data_dir) / sub, workers=a.workers)
            failed += errors
        if failed:
            print(f"\n[ERROR] {failed} file(s) failed to convert; fix or remove them and rerun")
            return 1

    ok = True
    if do_papers:
        try:
            with PapersCacheBuilder(a.data_dir, a.cache_dir, settings, a.papers_db) as b:
                if a.incremental:
                    done = b.incremental_update(workers=a.workers)
                else:
                    b.check_data_files()
                    done = b.create_optimized_papers_table(force_recreate=a.force)
                if done:
                    b.show_table_info()
                ok &= bool(done)
        except Exception:
            traceback.print_exc()
            ok = False
    if do_citations:
        try:
            with CitationCacheBuilder(str(Path(a.data_dir) / "citations"), a.cache_dir,
                                      settings, a.citations_db) as b:
                done = b.create_optimized_cache_table(force_recreate=a.force)
                if done:
                    b.show_cache_info()
                ok &= bool(done)
        except Exception:
            traceback.print_exc()
            ok = False
    print("\n[OK] build complete" if ok else "\n[ERROR] build failed")
    return 0 if ok else 1


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="s2db", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"s2db {__version__}")
    p.add_argument("--log-level", default=env("LOG_LEVEL", "info"))
    sub = p.add_subparsers(dest="cmd", required=True)

    # download
    d = sub.add_parser("download", help="mirror S2AG datasets via the Datasets API")
    d.add_argument("--raw-dir", default=env("RAW_DIR", "raw"))
    d.add_argument("--datasets", nargs="+", default=env("DATASETS", " ".join(DEFAULT_DATASETS)).split())
    d.add_argument("--workers", type=int, default=8, help="parallel file downloads")
    d.add_argument("--api-key-file", default=env("S2_API_KEY_FILE"),
                   help="file holding the S2 API key (default: env S2_API_KEY)")
    d.add_argument("--watch", action="store_true", help="keep running, check every --interval")
    d.add_argument("--interval", type=int, default=3600)
    d.add_argument("--on-complete", default=env("ON_COMPLETE"),
                   help="shell command run after each completed release "
                        "(gets S2DB_RELEASE and S2DB_RAW_DIR), e.g. a sync script")
    d.add_argument("--webhook", default=env("WEBHOOK_URL"),
                   help="Discord/Slack-compatible webhook for notifications")

    # build
    b = sub.add_parser("build", help="convert jsonl.gz to parquet and build the DuckDB caches")
    b.add_argument("--papers", action="store_true")
    b.add_argument("--citations", action="store_true")
    b.add_argument("--all", action="store_true")
    b.add_argument("--raw-dir", default=env("RAW_DIR", "raw"))
    b.add_argument("--data-dir", default=env("DATA_DIR", "data"))
    b.add_argument("--cache-dir", default=env("CACHE_DIR", "cache"))
    b.add_argument("--papers-db", default=env("PAPERS_DB", PAPERS_DB))
    b.add_argument("--citations-db", default=env("CITATIONS_DB", CITATIONS_DB))
    b.add_argument("--convert", action="store_true",
                   help="convert new jsonl.gz files under --raw-dir first (already converted are skipped)")
    b.add_argument("--incremental", "-i", action="store_true",
                   help="papers only: apply new parquet files to the existing cache")
    b.add_argument("--force", action="store_true", help="rebuild even if tables exist")
    b.add_argument("--workers", type=int, default=8, help="parallel conversion workers")
    g = b.add_argument_group("DuckDB resources (default: detected from Slurm / cgroup / host)")
    g.add_argument("--memory-limit")
    g.add_argument("--threads", type=int)
    g.add_argument("--temp-dir")
    g.add_argument("--max-temp-size")

    # serve
    s = sub.add_parser("serve", help="HTTP SQL server")
    s.add_argument("--database", "-d", default=env("DATABASE", ":memory:"))
    s.add_argument("--attach", action="append", metavar="ALIAS=PATH",
                   default=[x for x in env("ATTACH", "").split() if x],
                   help="attach another database (read-only with --read-only), repeatable")
    s.add_argument("--host", default=env("HOST", "127.0.0.1"))
    s.add_argument("--port", "-p", type=int, default=int(env("PORT", "8300")))
    s.add_argument("--api-key", default=env("API_KEY"))
    s.add_argument("--api-key-file", default=env("API_KEY_FILE"))
    s.add_argument("--read-only", action="store_true")
    s.add_argument("--threads", type=int)
    s.add_argument("--memory-limit")
    s.add_argument("--max-concurrency", type=int, default=8)
    s.add_argument("--max-rows", type=int, default=1_000_000)
    s.add_argument("--timeout", type=float, default=600.0)
    s.add_argument("--init-sql", help="SQL file run once at startup")
    s.add_argument("--no-external-access", action="store_true",
                   help="forbid queries from reading files / URLs (applied after --init-sql)")

    # query
    sub.add_parser("query", help="query a server (same as the standalone ddq.py)", add_help=False)

    # warmup
    w = sub.add_parser("warmup", help="pull hot columns and index paths into memory")
    w.add_argument("--url", default=env("URL", "http://127.0.0.1:8300"))
    w.add_argument("--top", type=int, default=500)
    w.add_argument("--workers", type=int, default=4)

    # verify
    v = sub.add_parser("verify", help="compare a new cache against the current one")
    v.add_argument("new_cache_dir")
    v.add_argument("current_cache_dir")
    v.add_argument("--papers-db", default=env("PAPERS_DB", PAPERS_DB))
    v.add_argument("--citations-db", default=env("CITATIONS_DB", CITATIONS_DB))
    v.add_argument("--max-shrink", type=float, default=0.03)
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # `query` forwards everything to ddq so its own --help/flags work unchanged
    if argv[:1] == ["query"]:
        from s2duckdb import ddq
        ddq.main(argv[1:])
        return 0

    a = build_parser().parse_args(argv)
    logging.basicConfig(level=a.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.cmd == "download":
        from s2duckdb import download
        return download.main(a)
    if a.cmd == "build":
        return cmd_build(a)
    if a.cmd == "serve":
        from s2duckdb import server
        return server.main(a)
    if a.cmd == "warmup":
        from s2duckdb import warmup
        return warmup.main(a)
    if a.cmd == "verify":
        from s2duckdb import verify
        return verify.main(a)
    return 2


if __name__ == "__main__":
    sys.exit(main())
