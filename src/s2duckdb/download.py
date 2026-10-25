"""
Keep a local mirror of S2AG datasets up to date via the S2 Datasets API.

Layout under ``raw_dir`` (what ``s2db build --convert`` expects)::

    <dataset>/          update and full-release files (jsonl.gz)
    <dataset>-deletes/  delete files of incremental releases
    .s2db/state.json    last fully synced release
    .s2db/done/<release_id>   marker written after a release is complete

The first sync downloads the full latest release; later syncs apply the
diffs from the last synced release. A release only counts as synced (state +
marker updated, on-complete hook run) when every file of every dataset is
downloaded, so an interrupted run simply resumes next time.

Files are written to ``<name>.part`` and renamed when complete, so a file
without the suffix is always whole. The API key is read from the
``S2_API_KEY`` environment variable (or ``--api-key-file``), never from code.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

API = "https://api.semanticscholar.org/datasets/v1"
log = logging.getLogger("s2db.download")


class S2Client:
    def __init__(self, api_key: Optional[str], retries: int = 6, base_url: str = API):
        self.s = requests.Session()
        if api_key:
            self.s.headers["x-api-key"] = api_key
        self.retries = retries
        self.base_url = base_url

    def get_json(self, path: str) -> dict:
        backoff = 30
        for attempt in range(self.retries):
            r = self.s.get(self.base_url + path, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                wait = int(r.headers.get("Retry-After", backoff))
                log.warning("%s -> %d, retry in %ds", path, r.status_code, wait)
                time.sleep(wait)
                backoff = min(backoff * 2, 600)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"{path}: giving up after {self.retries} attempts")

    def latest_release(self) -> str:
        return self.get_json("/release/latest")["release_id"]

    def release_files(self, release: str, dataset: str) -> List[str]:
        return self.get_json(f"/release/{release}/dataset/{dataset}")["files"]

    def diffs(self, start: str, end: str, dataset: str) -> List[dict]:
        return self.get_json(f"/diffs/{start}/to/{end}/{dataset}")["diffs"]


def file_name(url: str) -> str:
    """Base name of a (pre-signed) S3 URL, without the query string."""
    return os.path.basename(urlparse(url).path)


def download(session: requests.Session, url: str, dest: Path, retries: int = 5) -> Path:
    """Download url to dest via dest.part (resumable); skip if dest exists."""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, retries + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with session.get(url, stream=True, headers=headers, timeout=120) as r:
                if r.status_code == 416:  # .part already complete or corrupt
                    part.unlink(missing_ok=True)
                    continue
                r.raise_for_status()
                mode = "ab" if have and r.status_code == 206 else "wb"
                with open(part, mode) as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
            part.replace(dest)
            return dest
        except (requests.RequestException, OSError) as e:
            log.warning("%s attempt %d/%d failed: %s", dest.name, attempt, retries, e)
            time.sleep(min(2 ** attempt, 60))
    raise RuntimeError(f"failed to download {dest.name}")


class Mirror:
    def __init__(self, raw_dir: str, client: S2Client, datasets: Iterable[str], workers: int = 8):
        self.raw = Path(raw_dir)
        self.client = client
        self.datasets = list(datasets)
        self.workers = workers
        self.meta = self.raw / ".s2db"
        self.state_file = self.meta / "state.json"
        # S3 downloads do not need the API key header
        self.dl = requests.Session()

    # ---- state ----
    def last_release(self) -> Optional[str]:
        try:
            return json.loads(self.state_file.read_text())["release_id"]
        except (OSError, ValueError, KeyError):
            return None

    def _mark_done(self, release: str, files: List[Path]):
        self.meta.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"release_id": release, "synced_at": time.strftime("%FT%T%z")}))
        tmp.replace(self.state_file)
        done = self.meta / "done"
        done.mkdir(exist_ok=True)
        rel = [str(f.relative_to(self.raw)) for f in files]
        (done / release).write_text("\n".join(rel) + ("\n" if rel else ""))

    # ---- planning ----
    def plan(self, latest: str) -> List[Tuple[str, Path]]:
        """(url, destination) for every file needed to reach `latest`."""
        last = self.last_release()
        jobs: List[Tuple[str, Path]] = []
        for ds in self.datasets:
            if last is None:
                urls = self.client.release_files(latest, ds)
                jobs += [(u, self.raw / ds / file_name(u)) for u in urls]
                log.info("%s: full release %s, %d files", ds, latest, len(urls))
            else:
                diffs = self.client.diffs(last, latest, ds)
                n_up = n_del = 0
                for d in diffs:
                    jobs += [(u, self.raw / ds / file_name(u)) for u in d.get("update_files", [])]
                    jobs += [(u, self.raw / f"{ds}-deletes" / file_name(u))
                             for u in d.get("delete_files", [])]
                    n_up += len(d.get("update_files", []))
                    n_del += len(d.get("delete_files", []))
                log.info("%s: %s -> %s, %d diffs, %d update + %d delete files",
                         ds, last, latest, len(diffs), n_up, n_del)
        return jobs

    # ---- sync ----
    def sync_once(self) -> Optional[str]:
        """Bring the mirror to the latest release. Returns it if anything changed."""
        latest = self.client.latest_release()
        last = self.last_release()
        if latest == last:
            log.info("up to date (%s)", latest)
            return None
        log.info("new release %s (have %s)", latest, last or "nothing")
        jobs = self.plan(latest)
        with ThreadPoolExecutor(self.workers) as ex:
            files = list(ex.map(lambda j: download(self.dl, *j), jobs))
        self._mark_done(latest, files)
        log.info("release %s complete: %d files", latest, len(files))
        return latest


def notify(webhook: Optional[str], text: str):
    """POST to a Discord/Slack-compatible webhook, if configured."""
    if not webhook:
        return
    try:
        requests.post(webhook, json={"content": text, "text": text}, timeout=20)
    except requests.RequestException as e:
        log.warning("webhook failed: %s", e)


def run_hook(cmd: Optional[str], release: str, raw_dir: str) -> bool:
    if not cmd:
        return True
    env = dict(os.environ, S2DB_RELEASE=release, S2DB_RAW_DIR=str(raw_dir))
    rc = subprocess.call(cmd, shell=True, env=env)
    if rc != 0:
        log.error("on-complete hook exited %d", rc)
    return rc == 0


def main(args) -> int:
    api_key = os.environ.get("S2_API_KEY")
    if args.api_key_file:
        api_key = Path(args.api_key_file).expanduser().read_text().strip()
    if not api_key:
        log.warning("no S2_API_KEY: dataset downloads need an API key")
    mirror = Mirror(args.raw_dir, S2Client(api_key), args.datasets, args.workers)

    while True:
        try:
            release = mirror.sync_once()
            if release:
                ok = run_hook(args.on_complete, release, args.raw_dir)
                notify(args.webhook, f"S2 release {release} synced"
                       + ("" if ok else " (on-complete hook FAILED)"))
        except Exception as e:  # keep the daemon alive; retry next round
            log.exception("sync failed")
            notify(args.webhook, f"S2 sync failed: {e}")
            if not args.watch:
                return 1
        if not args.watch:
            return 0
        time.sleep(args.interval)
