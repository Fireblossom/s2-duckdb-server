"""Warm the DuckDB buffer pool / page cache after the server (re)starts.

deploy/slurm/serve.sbatch runs this in the background after every healthy
start (new job on a fresh node, or reload after a rebuild), so the first real
queries do not pay the cold-read cost. Safe to run by hand:

    S2DB_API_KEY=... s2db warmup [--url http://127.0.0.1:8300] [--top 500]

1. scan the columns most queries touch (papers metadata, citation keys)
2. look up the most-cited papers through the citation indexes, which loads
   the hot part of the ART indexes plus the matching rows
"""
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SCANS = [
    ("papers: hot columns",
     "SELECT max(corpusid), max(length(title)), max(year), count(venue), max(citationcount), "
     "count(externalids.DOI), count(externalids.ArXiv) FROM papers.papers_optimized"),
    ("citation_details: key columns",
     "SELECT max(citedcorpusid), max(citingcorpusid), count(*) FILTER (WHERE isinfluential) "
     "FROM citation_details"),
    ("citing_aggregated: key columns",
     "SELECT max(citingcorpusid), max(citation_count) FROM citing_aggregated"),
]


def q(base, key, sql, params=None, limit=1000, timeout=1800):
    body = json.dumps({"sql": sql, "params": params, "limit": limit, "timeout": timeout}).encode()
    req = urllib.request.Request(base + "/v1/query", body, {
        "Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=timeout + 60) as r:
        return json.loads(r.read())["rows"]


def log(msg):
    print(f"[warmup {time.strftime('%F %T')}] {msg}", flush=True)


def main(a) -> int:
    base, key = a.url.rstrip("/"), os.environ.get("S2DB_API_KEY", "")
    t_all = time.time()

    for name, sql in SCANS:
        t = time.time()
        try:
            q(base, key, sql)
            log(f"{name}: {time.time() - t:.1f}s")
        except Exception as e:
            log(f"{name}: FAILED {e}")

    t = time.time()
    ids = [r[0] for r in q(base, key,
        "SELECT corpusid FROM papers.papers_optimized ORDER BY citationcount DESC NULLS LAST LIMIT ?",
        [a.top], limit=a.top)]
    log(f"top {len(ids)} cited papers: {time.time() - t:.1f}s")

    def lookup(cid):
        q(base, key, "SELECT max(citingcorpusid) FROM citation_details WHERE citedcorpusid = ?", [cid])
        q(base, key, "SELECT max(citedcorpusid) FROM citation_details WHERE citingcorpusid = ?", [cid])
        q(base, key, "SELECT max(citation_count) FROM citing_aggregated WHERE citingcorpusid = ?", [cid])

    t, failed = time.time(), 0
    with ThreadPoolExecutor(a.workers) as ex:
        for f in [ex.submit(lookup, c) for c in ids]:
            try:
                f.result()
            except Exception:
                failed += 1
    log(f"index lookups for {len(ids)} papers: {time.time() - t:.1f}s, {failed} failed")
    log(f"done in {time.time() - t_all:.0f}s")
    return 0 if failed == 0 else 1

