"""raw jsonl.gz -> parquet -> DuckDB caches -> HTTP server -> queries."""

import argparse

import duckdb
import pytest
from fastapi.testclient import TestClient

from s2duckdb.cli import main as cli
from s2duckdb.server import build_app


@pytest.fixture
def cache(raw_dir, tmp_path):
    rc = cli(["build", "--all", "--convert", "--workers", "2",
              "--raw-dir", str(raw_dir), "--data-dir", str(tmp_path / "data"),
              "--cache-dir", str(tmp_path / "cache"), "--memory-limit", "1GB", "--threads", "2"])
    assert rc == 0
    return tmp_path / "cache"


def test_papers_cache(cache):
    con = duckdb.connect(str(cache / "papers.duckdb"), read_only=True)
    rows = dict(con.execute("SELECT corpusid, title FROM papers_optimized").fetchall())
    assert rows == {1: "Alpha", 2: "Beta new", 4: "Delta"}  # 3 deleted, 2 updated
    abstract, sha = con.execute(
        "SELECT abstract, sha_id FROM papers_optimized WHERE corpusid = 1").fetchone()
    assert (abstract, sha) == ("abs one", "s1")
    # s2fieldsofstudy categories are lower-cased by the builder
    assert con.execute("SELECT s2fieldsofstudy[1].category FROM papers_optimized "
                       "WHERE corpusid = 1").fetchone()[0] == "computer science"


def test_citations_cache(cache):
    con = duckdb.connect(str(cache / "citations.duckdb"), read_only=True)
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT citationid, contexts, isinfluential FROM citation_details").fetchall()}
    # 10 deleted, 12 has no contexts, 11 updated, 13 kept although the reduced
    # export both strips its contexts and lists it as deleted
    assert set(rows) == {11, 13}
    assert rows[11] == (["two cites one v2"], True)
    assert rows[13][0] == ["four cites one"]
    agg = con.execute("SELECT citation_count FROM citing_aggregated WHERE citingcorpusid = 2").fetchone()
    assert agg == (1,)


@pytest.fixture
def client(cache):
    args = argparse.Namespace(
        database=str(cache / "citations.duckdb"), attach=[f"papers={cache / 'papers.duckdb'}"],
        read_only=True, threads=2, memory_limit="1GB", max_concurrency=2, max_rows=100,
        timeout=10.0, init_sql=None, no_external_access=True, api_key="secret")
    with TestClient(build_app(args)) as c:
        yield c


AUTH = {"Authorization": "Bearer secret"}


def test_server_auth_and_tables(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/v1/tables").status_code == 401
    names = {t["table_name"] for t in client.get("/v1/tables", headers=AUTH).json()["tables"]}
    assert {"citation_details", "citing_aggregated", "papers_optimized"} <= names


def test_server_query_join(client):
    r = client.post("/v1/query", headers=AUTH, json={
        "sql": "SELECT p.title, list_transform(p.authors, lambda a: a.name) AS authors "
               "FROM citation_details c JOIN papers.papers_optimized p ON p.corpusid = c.citingcorpusid "
               "WHERE c.citedcorpusid = ? ORDER BY p.corpusid",
        "params": [1]})
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == [["Beta new", ["Author 2"]], ["Delta", ["Author 4"]]]


def test_server_read_only_and_sandbox(client):
    r = client.post("/v1/query", headers=AUTH, json={"sql": "DELETE FROM citation_details"})
    assert r.status_code == 400
    r = client.post("/v1/query", headers=AUTH, json={"sql": "SELECT * FROM read_csv('/etc/hosts')"})
    assert r.status_code == 400 and "Permission" in r.json()["error"]["message"]


def test_server_formats_and_limit(client):
    r = client.post("/v1/query", headers=AUTH, json={
        "sql": "SELECT * FROM range(10)", "format": "csv", "limit": 3})
    assert r.headers["x-truncated"] == "true" and r.text.count("\n") == 4
    import pyarrow as pa
    r = client.post("/v1/query", headers=AUTH, json={"sql": "SELECT 42 AS x", "format": "arrow"})
    assert pa.ipc.open_stream(r.content).read_all().to_pylist() == [{"x": 42}]


def test_verify(cache, tmp_path, capsys):
    assert cli(["verify", str(cache), str(cache)]) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli(["verify", str(empty), str(cache)]) == 1
