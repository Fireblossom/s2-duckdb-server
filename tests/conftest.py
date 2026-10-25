import gzip
import json
from pathlib import Path

import pytest

# Two releases. Names follow S2's YYYYMMDD_HHMMSS_NNNNN_xxxxx_<uuid> scheme,
# so name order is time order.
R1 = "20260101_000000_00001_aaaaa_0001"
R2 = "20260201_000000_00002_bbbbb_0002"
R2_REDUCED = "20260201_000500_00003_ccccc_0003"  # second export of R2, contexts stripped


def paper(cid, title, year=2020, cites=0):
    return {
        "corpusid": cid, "title": title, "year": year, "venue": "TestConf",
        "authors": [{"authorId": str(cid * 10), "name": f"Author {cid}"}],
        "externalids": {"ACL": None, "ArXiv": f"2001.{cid:05d}", "CorpusId": str(cid),
                        "DBLP": None, "DOI": f"10.1/{cid}", "MAG": None,
                        "PubMed": None, "PubMedCentral": None},
        "url": f"https://example.org/{cid}", "publicationvenueid": "v1",
        "referencecount": 1, "citationcount": cites, "influentialcitationcount": 0,
        "isopenaccess": True,
        "s2fieldsofstudy": [{"category": "Computer Science", "source": "s2"}],
        "publicationtypes": ["Conference"], "publicationdate": f"{year}-01-01",
        "journal": {"name": "J", "pages": "1-2", "volume": "1"},
    }


def citation(cid, citing, cited, contexts, influential=False):
    return {"citationid": cid, "citingcorpusid": citing, "citedcorpusid": cited,
            "isinfluential": influential, "contexts": contexts,
            "intents": [["background"] for _ in contexts] if contexts else None}


def write(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def raw_dir(tmp_path):
    raw = tmp_path / "raw"
    # release 1 (full)
    write(raw / "papers" / f"{R1}.gz", [paper(1, "Alpha", cites=5), paper(2, "Beta old"), paper(3, "Gamma")])
    write(raw / "abstracts" / f"{R1}.gz", [{"corpusid": 1, "abstract": "abs one"},
                                           {"corpusid": 2, "abstract": "abs two"}])
    write(raw / "paper-ids" / f"{R1}.gz", [{"sha": "s1", "corpusid": 1, "primary": True},
                                           {"sha": "s2", "corpusid": 2, "primary": True}])
    write(raw / "citations" / f"{R1}.gz", [
        citation(10, 1, 2, ["one cites two"]),
        citation(11, 2, 1, ["two cites one"]),
        citation(12, 1, 3, None),                       # no contexts: never cached
    ])
    # release 2 (incremental): update paper 2, add 4, delete 3; update citation 11,
    # add 13, delete 10. The reduced second export must not undo the full one.
    write(raw / "papers" / f"{R2}.gz", [paper(2, "Beta new"), paper(4, "Delta", cites=9)])
    write(raw / "papers-deletes" / f"{R2}.gz", [{"corpusid": 3}])
    write(raw / "citations" / f"{R2}.gz", [citation(11, 2, 1, ["two cites one v2"], True),
                                           citation(13, 4, 1, ["four cites one"])])
    write(raw / "citations" / f"{R2_REDUCED}.gz", [citation(13, 4, 1, None)])
    write(raw / "citations-deletes" / f"{R2}.gz", [{"citationid": 10}])
    write(raw / "citations-deletes" / f"{R2_REDUCED}.gz", [{"citationid": 13}])
    # in-progress download must be ignored
    (raw / "papers" / f"{R2}_x.gz.part").write_bytes(b"partial")
    return raw
