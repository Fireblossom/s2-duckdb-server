"""Downloader against a fake S2 Datasets API + S3 served from localhost."""

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from s2duckdb.download import Mirror, S2Client, run_hook

FILES = {
    "/s3/p_full_1.gz": gzip.compress(b'{"corpusid": 1}\n'),
    "/s3/p_full_2.gz": gzip.compress(b'{"corpusid": 2}\n'),
    "/s3/p_upd_1.gz": gzip.compress(b'{"corpusid": 3}\n'),
    "/s3/p_del_1.gz": gzip.compress(b'{"corpusid": 1}\n'),
}


@pytest.fixture
def fake_s2():
    state = {"latest": "2026-01-01", "fail_once": set(), "key_seen": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            base = f"http://127.0.0.1:{self.server.server_port}"
            path = self.path.split("?")[0]
            state["key_seen"].append(self.headers.get("x-api-key"))
            if path == "/api/release/latest":
                return self._json({"release_id": state["latest"]})
            if path == "/api/release/2026-01-01/dataset/papers":
                return self._json({"files": [f"{base}/s3/p_full_1.gz?sig=a", f"{base}/s3/p_full_2.gz?sig=b"]})
            if path == "/api/diffs/2026-01-01/to/2026-01-08/papers":
                return self._json({"diffs": [{"from_release": "2026-01-01", "to_release": "2026-01-08",
                                              "update_files": [f"{base}/s3/p_upd_1.gz?sig=c"],
                                              "delete_files": [f"{base}/s3/p_del_1.gz?sig=d"]}]})
            if path in FILES:
                if path in state["fail_once"]:
                    state["fail_once"].discard(path)
                    self.send_response(500)
                    self.end_headers()
                    return
                body = FILES[path]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["api"] = f"http://127.0.0.1:{srv.server_port}/api"
    yield state
    srv.shutdown()


def test_full_then_incremental(fake_s2, tmp_path, monkeypatch):
    monkeypatch.setattr("s2duckdb.download.time.sleep", lambda s: None)
    raw = tmp_path / "raw"
    m = Mirror(str(raw), S2Client("k123", base_url=fake_s2["api"]), ["papers"], workers=2)

    fake_s2["fail_once"].add("/s3/p_full_2.gz")  # transient S3 error is retried
    assert m.sync_once() == "2026-01-01"
    assert sorted(p.name for p in (raw / "papers").iterdir()) == ["p_full_1.gz", "p_full_2.gz"]
    assert m.last_release() == "2026-01-01"
    assert (raw / ".s2db" / "done" / "2026-01-01").read_text().split() == [
        "papers/p_full_1.gz", "papers/p_full_2.gz"]

    assert m.sync_once() is None  # nothing new

    fake_s2["latest"] = "2026-01-08"
    assert m.sync_once() == "2026-01-08"
    assert (raw / "papers" / "p_upd_1.gz").exists()
    assert (raw / "papers-deletes" / "p_del_1.gz").exists()
    assert not list(raw.rglob("*.part"))
    assert m.last_release() == "2026-01-08"
    # the API key goes to the API only
    assert "k123" in fake_s2["key_seen"]


def test_failed_release_does_not_advance(fake_s2, tmp_path, monkeypatch):
    monkeypatch.setattr("s2duckdb.download.time.sleep", lambda s: None)
    del FILES["/s3/p_full_2.gz"]
    try:
        m = Mirror(str(tmp_path / "raw"), S2Client(None, base_url=fake_s2["api"]), ["papers"])
        with pytest.raises(RuntimeError):
            m.sync_once()
        assert m.last_release() is None
    finally:
        FILES["/s3/p_full_2.gz"] = gzip.compress(b'{"corpusid": 2}\n')


def test_hook_gets_release(tmp_path):
    out = tmp_path / "hook.txt"
    assert run_hook(f'echo "$S2DB_RELEASE $S2DB_RAW_DIR" > {out}', "2026-01-08", "/r")
    assert out.read_text().strip() == "2026-01-08 /r"
    assert not run_hook("exit 3", "x", "/r")


def test_skill_client_is_in_sync():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert (root / "src/s2duckdb/ddq.py").read_text() == \
        (root / "skills/s2-duckdb/scripts/ddq.py").read_text()
