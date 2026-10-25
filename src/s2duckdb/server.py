"""HTTP SQL server over read-only DuckDB caches (like ``vllm serve`` for SQL).

    s2db serve --database cache/citations.duckdb --attach papers=cache/papers.duckdb \
               --read-only --no-external-access --api-key-file ~/.s2db_key

Endpoints:
    GET  /health                      liveness check
    GET  /version                     server / DuckDB version
    GET  /v1/tables                   list tables and views
    GET  /v1/tables/{name}            columns of one table
    POST /v1/query                    run SQL, return json | csv | arrow
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

import duckdb
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from s2duckdb import __version__

log = logging.getLogger("s2db.serve")


# ---------------------------------------------------------------- engine

class QueryTimeout(Exception):
    pass


class Engine:
    """One shared DuckDB connection; every query runs on its own cursor."""

    def __init__(self, args: argparse.Namespace):
        config: dict[str, Any] = {}
        if args.threads:
            config["threads"] = args.threads
        if args.memory_limit:
            config["memory_limit"] = args.memory_limit
        read_only = args.read_only and args.database != ":memory:"
        self.conn = duckdb.connect(args.database, read_only=read_only, config=config)
        self.read_only = read_only
        self.database = args.database
        self.max_rows = args.max_rows
        self.default_timeout = args.timeout
        self.sem = asyncio.Semaphore(args.max_concurrency)
        for spec in args.attach or []:
            alias, _, path = spec.partition("=")
            if not path:
                raise ValueError(f"--attach expects ALIAS=PATH, got {spec!r}")
            mode = " (READ_ONLY)" if read_only else ""
            self.conn.execute(f"ATTACH '{path.replace(chr(39), chr(39) * 2)}' AS \"{alias}\"{mode}")
            log.info("attached %s as %s", path, alias)
        if args.init_sql:
            with open(args.init_sql, encoding="utf-8") as f:
                self.conn.execute(f.read())
            log.info("ran init SQL from %s", args.init_sql)
        if args.no_external_access:
            self.conn.execute("SET enable_external_access = false")
            self.conn.execute("SET lock_configuration = true")

    def _run(self, sql: str, params: list | dict | None, limit: int,
             timeout: float, fmt: str):
        cur = self.conn.cursor()
        timed_out = threading.Event()

        def _interrupt():
            timed_out.set()
            cur.interrupt()

        timer = threading.Timer(timeout, _interrupt)
        timer.start()
        try:
            rel = cur.execute(sql, params) if params else cur.execute(sql)
            if cur.description is None:  # DDL / DML with no result set
                return [], [], [], False
            columns = [d[0] for d in cur.description]
            types = [str(d[1]) for d in cur.description]
            if fmt == "arrow":
                # DuckDB 2.0 renamed fetch_record_batch to to_arrow_reader
                to_reader = getattr(rel, "to_arrow_reader", None) or rel.fetch_record_batch
                reader = to_reader(min(limit, 100_000))
                batches, n = [], 0
                for batch in reader:
                    batches.append(batch)
                    n += batch.num_rows
                    if n > limit:
                        break
                import pyarrow as pa
                table = pa.Table.from_batches(batches, schema=reader.schema)
                truncated = table.num_rows > limit
                return columns, types, table.slice(0, limit), truncated
            rows = cur.fetchmany(limit + 1)
            truncated = len(rows) > limit
            return columns, types, rows[:limit], truncated
        except duckdb.InterruptException as e:
            if timed_out.is_set():
                raise QueryTimeout(f"query exceeded {timeout}s timeout") from e
            raise
        finally:
            timer.cancel()
            cur.close()

    async def query(self, sql: str, params=None, limit: int | None = None,
                    timeout: float | None = None, fmt: str = "json"):
        limit = min(limit or self.max_rows, self.max_rows)
        timeout = timeout or self.default_timeout
        async with self.sem:
            return await asyncio.to_thread(self._run, sql, params, limit, timeout, fmt)


# ---------------------------------------------------------------- API models

class QueryRequest(BaseModel):
    sql: str = Field(..., description="SQL to execute")
    params: list[Any] | dict[str, Any] | None = Field(
        None, description="Bind parameters for ? or $name placeholders")
    format: Literal["json", "csv", "arrow"] = "json"
    limit: int | None = Field(None, ge=1, description="Max rows (capped by --max-rows)")
    timeout: float | None = Field(None, gt=0, description="Seconds (default --timeout)")


def _json_default(o: Any) -> Any:
    # Decimal, date/time, UUID, bytes, intervals... -> string
    if isinstance(o, (bytes, bytearray)):
        return o.hex()
    return str(o)


def dumps(obj: Any) -> bytes:
    return json.dumps(obj, default=_json_default, ensure_ascii=False).encode()


# ---------------------------------------------------------------- app

def build_app(args: argparse.Namespace) -> FastAPI:
    engine: Engine | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal engine
        engine = Engine(args)
        log.info("DuckDB %s opened %s (read_only=%s)",
                 duckdb.__version__, args.database, engine.read_only)
        yield
        engine.conn.close()

    app = FastAPI(title="DuckDB Query Server", version=__version__, lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def auth(cred: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if args.api_key and (cred is None or cred.credentials != args.api_key):
            raise HTTPException(401, "invalid or missing API key")

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        t0 = time.perf_counter()
        resp = await call_next(request)
        log.info("%s %s -> %d %.1fms", request.method, request.url.path,
                 resp.status_code, (time.perf_counter() - t0) * 1000)
        return resp

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/version")
    async def version():
        return {"server": __version__, "duckdb": duckdb.__version__,
                "database": args.database, "read_only": engine.read_only}

    @app.get("/v1/tables", dependencies=[Depends(auth)])
    async def tables():
        cols, _, rows, _ = await engine.query(
            "SELECT table_catalog, table_schema, table_name, table_type "
            "FROM information_schema.tables ORDER BY 1, 2, 3")
        return {"tables": [dict(zip(cols, r)) for r in rows]}

    @app.get("/v1/tables/{name}", dependencies=[Depends(auth)])
    async def table_schema(name: str, schema: str = "main"):
        cols, _, rows, _ = await engine.query(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [schema, name])
        if not rows:
            raise HTTPException(404, f"table {schema}.{name} not found")
        return {"table": name, "schema": schema,
                "columns": [dict(zip(cols, r)) for r in rows]}

    @app.post("/v1/query", dependencies=[Depends(auth)])
    async def query(req: QueryRequest):
        qid = uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        try:
            columns, types, data, truncated = await engine.query(
                req.sql, req.params, req.limit, req.timeout, req.format)
        except QueryTimeout as e:
            return JSONResponse({"id": qid, "error": {"type": "timeout", "message": str(e)}}, 408)
        except duckdb.Error as e:
            return JSONResponse(
                {"id": qid, "error": {"type": type(e).__name__, "message": str(e)}}, 400)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        headers = {"X-Query-Id": qid, "X-Elapsed-Ms": str(elapsed_ms),
                   "X-Truncated": str(truncated).lower()}

        if req.format == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(columns)
            w.writerows(data)
            return Response(buf.getvalue(), media_type="text/csv", headers=headers)

        if req.format == "arrow":
            import pyarrow as pa
            sink = pa.BufferOutputStream()
            with pa.ipc.new_stream(sink, data.schema) as writer:
                writer.write_table(data)
            return Response(sink.getvalue().to_pybytes(),
                            media_type="application/vnd.apache.arrow.stream",
                            headers=headers)

        body = {"id": qid, "columns": columns, "types": types,
                "rows": [list(r) for r in data], "row_count": len(data),
                "truncated": truncated, "elapsed_ms": elapsed_ms}
        return Response(dumps(body), media_type="application/json", headers=headers)

    return app


def main(args: argparse.Namespace) -> int:
    if args.api_key_file and not args.api_key:
        with open(os.path.expanduser(args.api_key_file), encoding="utf-8") as f:
            args.api_key = f.read().strip()
    if not args.api_key:
        log.warning("no API key set: anyone who can reach the port can query")
    if args.read_only and args.database == ":memory:":
        log.warning("--read-only ignored for in-memory database")
    uvicorn.run(build_app(args), host=args.host, port=args.port,
                log_level=args.log_level, access_log=False)
    return 0
