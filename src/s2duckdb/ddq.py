#!/usr/bin/env python3
"""ddq: command-line client for an s2db server (stdlib only, Python 3.8+).

Standalone on purpose: copy this one file to any machine that can reach the
server. Configure with S2DB_URL (default http://127.0.0.1:8300) and the API
key in S2DB_API_KEY or the file S2DB_KEY_FILE (default ~/.s2db_key).

    ddq.py "SELECT corpusid, title FROM papers.papers_optimized WHERE corpusid = 13756489"
    ddq.py -f sql/my_query.sql --format csv > out.csv
    ddq.py --tables
    ddq.py --schema papers_optimized
    ddq.py --health

Output: tab-separated table (default), or --format json / csv / jsonl.
Exit codes: 0 ok, 1 SQL error, 2 timeout, 3 service unreachable / auth.
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request

URL = os.environ.get("S2DB_URL", "http://127.0.0.1:8300")
KEY_FILE = os.path.expanduser(os.environ.get("S2DB_KEY_FILE", "~/.s2db_key"))


def _key():
    k = os.environ.get("S2DB_API_KEY")
    if k:
        return k
    try:
        with open(KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        die(3, "no API key: %s missing and S2DB_API_KEY unset" % KEY_FILE)


def die(code, msg):
    sys.stderr.write("ddq: %s\n" % msg)
    sys.exit(code)


def call(method, path, body=None, http_timeout=900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + _key())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except ValueError:
            payload = {"error": {"type": "http", "message": str(e)}}
        return e.code, payload
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        die(3, "service unreachable at %s (%s). The server or its tunnel may be "
               "restarting; wait 1-2 min and retry. Check: curl %s/health" % (URL, e, URL))


def cell(v):
    if v is None:
        return "NULL"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def emit(res, fmt, max_width):
    cols, rows = res["columns"], res["rows"]
    out = sys.stdout
    if fmt == "json":
        json.dump(res, out, ensure_ascii=False, indent=1)
        out.write("\n")
    elif fmt == "jsonl":
        for r in rows:
            out.write(json.dumps(dict(zip(cols, r)), ensure_ascii=False) + "\n")
    elif fmt == "csv":
        w = csv.writer(out)
        w.writerow(cols)
        for r in rows:
            w.writerow([cell(v) for v in r])
    else:
        def clip(s):
            s = s.replace("\t", " ").replace("\n", " ")
            return s if max_width <= 0 or len(s) <= max_width else s[:max_width - 1] + "…"
        out.write("\t".join(cols) + "\n")
        for r in rows:
            out.write("\t".join(clip(cell(v)) for v in r) + "\n")
    sys.stderr.write("-- %d rows%s, %.0f ms\n" % (
        res["row_count"], " (TRUNCATED, raise --limit or aggregate)" if res["truncated"] else "",
        res["elapsed_ms"]))


def main(argv=None):
    p = argparse.ArgumentParser(prog="ddq", description="Query an s2db server")
    p.add_argument("sql", nargs="?", help="SQL text (or use -f / stdin)")
    p.add_argument("-f", "--file", help="read SQL from file ('-' = stdin)")
    p.add_argument("-p", "--param", action="append", default=[],
                   help="positional bind parameter for ?, repeatable (all-digit -> int, else string)")
    p.add_argument("--format", choices=["tsv", "json", "jsonl", "csv"], default="tsv")
    p.add_argument("--limit", type=int, default=1000, help="max rows returned (default 1000)")
    p.add_argument("--timeout", type=float, default=300, help="server-side timeout seconds (default 300)")
    p.add_argument("--max-width", type=int, default=200, help="clip tsv cells (0 = no clip)")
    p.add_argument("--tables", action="store_true", help="list tables")
    p.add_argument("--schema", metavar="TABLE", help="show columns of TABLE")
    p.add_argument("--health", action="store_true")
    a = p.parse_args(argv)

    if a.health:
        code, res = call("GET", "/health", http_timeout=10)
        print(res)
        sys.exit(0 if code == 200 else 3)
    if a.tables:
        a.sql = ("SELECT database_name AS db, table_name, estimated_size AS approx_rows "
                 "FROM duckdb_tables() ORDER BY 1, 2")
    elif a.schema:
        a.sql = ("SELECT table_catalog AS db, column_name, data_type FROM information_schema.columns "
                 "WHERE table_name = ? ORDER BY table_catalog, ordinal_position")
        a.param = [a.schema]
    elif a.file:
        a.sql = sys.stdin.read() if a.file == "-" else open(a.file).read()
    elif not a.sql:
        if sys.stdin.isatty():
            p.error("give SQL as argument, -f FILE, or on stdin")
        a.sql = sys.stdin.read()

    # integers (corpusids) bind as int; everything else as string,
    # so ArXiv ids like 1706.03762 or DOIs are never turned into floats
    params = [int(s) if re.fullmatch(r"-?\d+", s) else s for s in a.param]

    body = {"sql": a.sql, "limit": a.limit, "timeout": a.timeout, "format": "json"}
    if params:
        body["params"] = params
    code, res = call("POST", "/v1/query", body, http_timeout=a.timeout + 60)
    if code == 200:
        emit(res, a.format, a.max_width)
        return
    err = res.get("error") or res.get("detail") or res
    if isinstance(err, dict):
        msg = "%s: %s" % (err.get("type"), err.get("message"))
    else:
        msg = str(err)
    if code == 408:
        die(2, msg + "  -> narrow the query (indexed column, LIMIT, smaller year range) or raise --timeout")
    if code == 401:
        die(3, "auth failed (check %s)" % KEY_FILE)
    die(1, msg)


if __name__ == "__main__":
    main()
