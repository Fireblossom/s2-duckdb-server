# s2-duckdb-server

[中文说明](README.zh-CN.md)

Keep a local copy of the [Semantic Scholar Academic Graph](https://www.semanticscholar.org/product/api#datasets)
up to date, build fast DuckDB caches from it, and serve SQL over HTTP — the way
`vllm serve` exposes a model, but for ~240M papers and ~1.1B citations with
their context sentences.

```
S2 Datasets API ──s2db download──▶ raw/*.gz ──s2db build──▶ cache/*.duckdb ──s2db serve──▶ HTTP :8300
   (weekly releases, diffs)       (+ deletes)   (parquet, dedup,            (read-only,        │
                                                 deletes applied)            API key)          ▼
                                                                              ddq.py / agents / curl
```

What you get:

- **`s2db download`** — mirrors the `papers`, `abstracts`, `paper-ids` and `citations`
  datasets: the full release once, then the weekly diffs (update and delete files).
  Resumable, atomic (`.part` then rename), and a release only counts as synced when
  every file is there. Optional on-complete hook and webhook notification.
- **`s2db build`** — converts jsonl.gz to parquet and builds two DuckDB caches.
  For every record the newest version wins, S2 delete files are applied, and
  citations without context are dropped (see [How the caches are built](#how-the-caches-are-built)).
- **`s2db serve`** — FastAPI server: JSON / CSV / Arrow results, bearer-token auth,
  per-query timeout, concurrency limit, row cap, read-only, no file/URL access.
- **`ddq.py`** — a one-file, stdlib-only client, and an [agent skill](skills/s2-duckdb/SKILL.md)
  that teaches LLM agents the schema and the queries that are fast.
- **`deploy/`** — Slurm jobs that keep the server up indefinitely, rebuild
  automatically when new data arrives, swap caches with a few seconds of downtime,
  warm the cache after every start, and optional SSH tunnels to reach a compute node.

## Install

```bash
git clone https://github.com/Fireblossom/s2-duckdb-server.git
cd s2-duckdb-server
python3 -m venv .venv && .venv/bin/pip install -e .
```

Python ≥ 3.10. Works with DuckDB 1.4, 1.5 and 2.0 (tests run on 1.5 and 2.0).
Database files written by DuckDB 2.0 cannot be opened by 1.x, so build and
serve with the same major version.

## Quick start

```bash
export S2_API_KEY=...            # https://www.semanticscholar.org/product/api#api-key-form

# 1. download (first run: the full release, several hundred GB)
s2db download --raw-dir raw
s2db download --raw-dir raw --watch --interval 3600      # keep following new releases

# 2. build both caches
s2db build --all --convert --raw-dir raw --data-dir data --cache-dir cache

# 3. serve
echo "$(openssl rand -hex 24)" > ~/.s2db_key && chmod 600 ~/.s2db_key
s2db serve --database cache/citations.duckdb --attach papers=cache/papers.duckdb \
           --read-only --no-external-access --api-key-file ~/.s2db_key

# 4. query
s2db query "SELECT title, year FROM papers.papers_optimized WHERE externalids.ArXiv = '1706.03762'"
curl -s localhost:8300/v1/query -H "Authorization: Bearer $(cat ~/.s2db_key)" \
     -H 'Content-Type: application/json' -d '{"sql": "SELECT count(*) FROM citation_details"}'
```

Resources: the full citations build needs about 1.2 TB of RAM (papers about 1 TB)
and takes roughly an hour on 64 cores; the caches take ~1.2 TB of disk. Memory,
threads and the spill directory are detected from the Slurm allocation / cgroup /
host, or set them with `--memory-limit`, `--threads`, `--temp-dir`.

## HTTP API

| method | path | |
|---|---|---|
| GET | `/health` | liveness, no auth |
| GET | `/version` | server and DuckDB version |
| GET | `/v1/tables` | tables and views |
| GET | `/v1/tables/{name}?schema=main` | columns of a table |
| POST | `/v1/query` | `{"sql", "params", "format": "json"\|"csv"\|"arrow", "limit", "timeout"}` |
| GET | `/docs` | OpenAPI UI |

Responses carry `X-Query-Id`, `X-Elapsed-Ms` and `X-Truncated`. SQL errors are 400,
timeouts 408, missing or wrong key 401.

## Tables

| table | rows (Oct 2026) | key columns |
|---|---|---|
| `papers.papers_optimized` | 239M | `corpusid` (PK), title, year, venue, authors, abstract, externalids (DOI, ArXiv, …), citationcount |
| `citation_details` | 1.1B | `citingcorpusid`, `citedcorpusid` (both indexed), isinfluential, contexts, intents, citationid |
| `citing_aggregated` | 53M | `citingcorpusid` (indexed), citation_count, all_cited_papers, … |

Full schema, performance rules and query templates: [skills/s2-duckdb/SKILL.md](skills/s2-duckdb/SKILL.md).

## Running it on a Slurm cluster

```bash
cp deploy/s2db.env.example deploy/s2db.env    # paths, account, partition, memory, relay
deploy/slurm/start.sh                         # starts s2db-serve and s2db-watch
```

- **s2db-serve** keeps the server up: restarts it if it dies, reloads on request
  (`echo reload > $S2DB_STATE_DIR/serve.ctrl`; `hold` stops it but keeps the
  allocation), runs `s2db warmup` after every start, and queues its own successor
  so it survives the time limit.
- **s2db-watch** runs [`check.sh`](deploy/slurm/check.sh) every 15 minutes. When
  `raw/` has files the live cache was not built from and the sync is finished
  (a release marker arrived, or no new file for `QUIET_MIN` minutes), it builds both
  caches into a staging directory, verifies row counts (`s2db verify`), swaps them
  in, keeps the previous version in `cache/prev_auto/` and makes the server reload.
  A failed round leaves the live cache untouched and is retried after 24 h.
  Progress: `$S2DB_STATE_DIR/auto/status.log`.

Compute nodes are usually unreachable from outside. Set `RELAY` in `s2db.env` and
the serve job keeps a reverse SSH tunnel to that host; consumers forward a local
port to it with [`deploy/tunnel/forward.sh`](deploy/tunnel/forward.sh), kept alive by
[`watchdog.sh`](deploy/tunnel/watchdog.sh) from cron:

```
compute node 127.0.0.1:8300 ──ssh -R──▶ relay 127.0.0.1:18300 ◀──ssh -L── consumer 127.0.0.1:8300
```

If the machine with internet access is not the cluster (e.g. a small VM downloads,
the cluster builds), run the downloader there with
`--on-complete "deploy/sync/rclone-push.sh --delete cluster:/data/s2/raw"`: it copies
the new files, verifies them, copies the release marker last (so the cluster starts
building as soon as it sees it), then frees the local copy.

## How the caches are built

- S2 file names start with a timestamp, so name order is time order. For each
  `corpusid` / `sha` / `citationid` the row from the newest file wins.
- Delete files (`<dataset>-deletes/`) drop a key when the delete is newer than
  the kept version; papers deleted from `abstracts` keep their metadata.
- Every citations release comes as two exports a few minutes apart; the second
  repeats the citations with the contexts of publisher-restricted papers removed.
  The builder keeps the newest version **with** contexts and applies only the
  first export's deletes, so the reduced export cannot erase contexts.
- `citation_details` is sorted by `citingcorpusid` and built in 16 id ranges to
  bound memory; ART indexes on both corpus id columns make edge lookups fast.

The build code comes from
[citation-context-dataset-discovery](https://github.com/Fireblossom/citation-context-dataset-discovery)
(JCDL 2025), which uses these caches for citation-grounded dataset discovery.

## Development

```bash
.venv/bin/pip install -e ".[test]" && .venv/bin/pytest
```

The tests build both caches from a small synthetic release pair (updates, deletes,
the reduced second export), serve them and query over HTTP, and run the downloader
against a fake Datasets API.

## License

MIT. The Semantic Scholar data itself is subject to the
[S2 API license agreement](https://www.semanticscholar.org/product/api/license).
