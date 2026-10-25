---
name: s2-duckdb
description: Query the Semantic Scholar Academic Graph (≈240M papers, ≈1.1B citations with citation-context sentences and intents) through an s2db DuckDB SQL server. Use for paper metadata (title, authors, year, venue, DOI/ArXiv, abstract, citation counts), who cites whom, citation context sentences, citation intents (background/methodology/result), lookups by corpusid, and statistics over papers or citations.
---

# S2AG over DuckDB (s2db server)

A read-only SQL service over Semantic Scholar data. Default endpoint
`http://127.0.0.1:8300` (override with `S2DB_URL`), API key in `~/.s2db_key`
(or `S2DB_API_KEY`). Only `SELECT` works: writes and file access are disabled.

## How to query

Use the bundled client (Python 3.8+, stdlib only):

```bash
DDQ=~/.claude/skills/s2-duckdb/scripts/ddq.py
python3 $DDQ "SELECT corpusid, title, year FROM papers.papers_optimized WHERE corpusid = 13756489"
python3 $DDQ -p 13756489 "SELECT count(*) FROM citation_details WHERE citedcorpusid = ?"
python3 $DDQ --format csv --limit 100000 -f query.sql > out.csv
python3 $DDQ --tables
python3 $DDQ --schema citation_details
python3 $DDQ --health
```

- TSV on stdout; row count, time and TRUNCATED warnings on stderr.
- `--format json|jsonl|csv`, `--limit` (default 1000), `--timeout` seconds (default 300).
- Bind user-supplied values with `-p` and `?` placeholders; never build SQL strings from them.
  All-digit values bind as integers, everything else as strings.
- Exit codes: 0 ok, 1 SQL error, 2 timeout, 3 server unreachable / auth failure.

From Python without the script:

```python
import json, os, urllib.request
def ddq(sql, params=None, limit=1000, timeout=300):
    body = {"sql": sql, "params": params, "limit": limit, "timeout": timeout}
    key = os.environ.get("S2DB_API_KEY") or open(os.path.expanduser("~/.s2db_key")).read().strip()
    req = urllib.request.Request(os.environ.get("S2DB_URL", "http://127.0.0.1:8300") + "/v1/query",
        json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": "Bearer " + key})
    r = json.loads(urllib.request.urlopen(req, timeout=timeout + 60).read())
    return [dict(zip(r["columns"], row)) for row in r["rows"]]
```

## Tables

Citation tables are in the main database; the paper table is in the attached
database `papers` — **always write `papers.papers_optimized`**.

### `papers.papers_optimized` — paper metadata (≈239M rows)
| column | type | notes |
|---|---|---|
| corpusid | BIGINT | Semantic Scholar CorpusId; primary key, joins everything |
| title | VARCHAR | |
| year | INTEGER | |
| venue | VARCHAR | |
| authors | STRUCT(authorId, name)[] | names: `list_transform(authors, lambda a: a.name)` |
| abstract | VARCHAR | '' when unknown |
| externalids | STRUCT(ACL, ArXiv, CorpusId, DBLP, DOI, MAG, PubMed, PubMedCentral) | `externalids.DOI`, `externalids.ArXiv` |
| url, sha_id, publicationvenueid | VARCHAR | |
| referencecount, citationcount, influentialcitationcount | INTEGER | counts from S2 |
| isopenaccess | BOOLEAN | |
| s2fieldsofstudy | STRUCT(category, source)[] | category lower-cased |
| publicationtypes | VARCHAR[] | |
| publicationdate | VARCHAR | 'YYYY-MM-DD' |
| journal | STRUCT(name, pages, volume) | |
| created_at | TIMESTAMP | when the row entered the cache |

### `citation_details` — citation edges with context (≈1.1B rows)
| column | type | notes |
|---|---|---|
| citingcorpusid | BIGINT | citing paper (indexed) |
| citedcorpusid | BIGINT | cited paper (indexed) |
| isinfluential | BOOLEAN | |
| contexts | VARCHAR[] | sentences containing the citation |
| intents | VARCHAR[][] | per context, e.g. `[['background'], ['methodology']]` |
| citationid | BIGINT | |

Only citations that have at least one context are stored.

### `citing_aggregated` — per citing paper (≈53M rows)
| column | type | notes |
|---|---|---|
| citingcorpusid | BIGINT | indexed |
| citation_count, unique_cited_count, influential_count | BIGINT | |
| all_cited_papers | VARCHAR | **comma-separated string**: `string_split(all_cited_papers, ',')` |
| all_contexts, all_intents | VARCHAR | serialized lists |

## Performance rules

Measured on the full dataset (warm server):

| query shape | time |
|---|---|
| `papers.papers_optimized WHERE corpusid = ?` | ~0.05 s |
| `citation_details WHERE citedcorpusid = ?` / `citingcorpusid = ?` | ~0.1–0.5 s (very highly cited paper + join: ~20 s) |
| `citing_aggregated WHERE citingcorpusid = ?` | ~0.05 s |
| `WHERE externalids.DOI = ?` / `externalids.ArXiv = ?` | ~0.3 s |
| `title ILIKE '%...%'` (full scan of 240M rows) | ~8 s |
| unfiltered aggregation over `citation_details` (1.1B rows) | minutes, may time out |

1. **Get the corpusid first, then use it for everything.** From a title: try
   `lower(title) = lower(?)`, then `ILIKE`, with `ORDER BY citationcount DESC LIMIT 10`
   to pick the right paper. From a DOI/ArXiv id: filter on `externalids` (if a DOI is
   not found, retry with `upper(externalids.DOI) = upper(?)`).
2. **Citation queries must filter on `citedcorpusid` or `citingcorpusid`.** No unfiltered
   GROUP BY / ORDER BY over `citation_details`.
3. Select only the columns you need; `abstract`, `contexts` and `authors` are large.
4. When a result may be big, run `count(*)` first; a TRUNCATED warning means raise
   `--limit` or aggregate.
5. Many ids: one query with `WHERE corpusid IN (...)`, not hundreds of requests. The
   server runs at most 8 queries at a time.
6. Heavy analysis: test on `USING SAMPLE 1%` or a single year first; keep `--timeout` ≤ 600.

## Templates

```sql
-- find a paper by title
SELECT corpusid, title, year, venue, citationcount
FROM papers.papers_optimized WHERE title ILIKE ? ORDER BY citationcount DESC LIMIT 10;
-- -p '%attention is all you need%'

-- who cites X (with contexts and intents, most-cited citing papers first)
SELECT p.corpusid, p.title, p.year, c.isinfluential, c.contexts, c.intents
FROM citation_details c JOIN papers.papers_optimized p ON p.corpusid = c.citingcorpusid
WHERE c.citedcorpusid = ? ORDER BY p.citationcount DESC LIMIT 50;

-- what X cites
SELECT p.corpusid, p.title, p.year
FROM citation_details c JOIN papers.papers_optimized p ON p.corpusid = c.citedcorpusid
WHERE c.citingcorpusid = ?;

-- intent distribution of citations to X
SELECT intent, count(*) AS n
FROM (SELECT unnest(flatten(intents)) AS intent FROM citation_details WHERE citedcorpusid = ?)
GROUP BY 1 ORDER BY n DESC;

-- citations to X per year
SELECT p.year, count(*) AS n
FROM citation_details c JOIN papers.papers_optimized p ON p.corpusid = c.citingcorpusid
WHERE c.citedcorpusid = ? GROUP BY 1 ORDER BY 1;

-- author names and DOI
SELECT title, list_transform(authors, lambda a: a.name) AS authors, externalids.DOI AS doi
FROM papers.papers_optimized WHERE corpusid = ?;
```

## When something fails

- **Exit 3 / connection refused**: the server or its tunnel is restarting (long-running
  jobs rotate, rebuilds reload). Wait 1–2 minutes and retry `--health`; only report to
  the user if it stays down for ~10 minutes. Do not try to fix tunnels or jobs yourself.
- **Exit 2 / timeout**: the query is too heavy; narrow it per the rules above instead of
  only raising the timeout.
- **Catalog Error: Table ... does not exist**: usually a missing `papers.` prefix.
- **read-only / Permission Error**: by design; no INSERT/CREATE/COPY/read_csv.
- The data is a snapshot of S2 (see `created_at`), updated when S2 publishes a release.
