"""Semantic Scholar Academic Graph -> DuckDB: download, build, serve."""

__version__ = "0.1.0"

# File names of the two caches inside the cache directory.
PAPERS_DB = "papers.duckdb"
CITATIONS_DB = "citations.duckdb"

# S2 datasets used by each cache, and their delete counterparts.
PAPERS_DATASETS = ["papers", "abstracts", "paper-ids"]
CITATIONS_DATASETS = ["citations"]
DEFAULT_DATASETS = PAPERS_DATASETS + CITATIONS_DATASETS
