# Ported from Fireblossom/citation-context-dataset-discovery (MIT), src/cache/citation_cache.py
"""
Citation Cache Builder

Builds and queries the citation cache (tables ``citation_details`` and
``citing_aggregated``) from Semantic Scholar ``citations`` parquet files.

The input directory may hold a full S2 release plus later incremental update
files. Every citation is keyed by ``citationid`` and only one version is kept,
so updates replace rows instead of being counted twice: the newest version that
has contexts (citations without contexts are not cached at all).

Preferring versions with contexts matters because S2 ships every citations
release (full and incremental) as two exports a few minutes apart, and the
later one repeats the same citations with the contexts of publisher-restricted
citing papers removed (the Graph API still serves them via /citations).

Citation IDs listed in S2 delete files (``<data_dir>-deletes/``) are dropped
when the delete is newer than the kept version. Deletes come in the same two
exports; only the first (full) one is applied, because the later one lists
citations that merely left the reduced export: it deletes citations that the
full export of the same release still updates.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from s2duckdb import CITATIONS_DB
from s2duckdb.build.base import (
    FILE_BASENAME,
    DuckDBCache,
    DuckDBSettings,
    create_latest_deletes_table,
    deletes_dir_for,
    list_parquet_files,
    sql_literal,
)

# Range of citingcorpusid values aggregated per INSERT. citation_details is
# sorted by citingcorpusid, so each range only reads its own row groups.
DEFAULT_ID_BATCH_SIZE = 5_000_000

# Number of citingcorpusid ranges citation_details is built in. Each range
# rescans the parquet files but only has to deduplicate and sort its share of
# the data; one pass over everything exhausted 1.2 TB of DuckDB memory.
DEFAULT_DETAIL_PARTITIONS = 16


class CitationCacheBuilder(DuckDBCache):
    """
    Optimized cache builder for citation contexts.

    Provides:
    - Table creation from parquet files
    - Batch query for citation contexts by citing paper
    - Batch query for papers that cite a given paper
    """

    def __init__(
        self,
        data_dir: str = "data/citations",
        cache_dir: str = "cache",
        settings: Optional[DuckDBSettings] = None,
        db_name: str = CITATIONS_DB,
    ):
        """
        Args:
            data_dir: Directory with citation parquet files
            cache_dir: Directory holding the DuckDB cache database
            settings: DuckDB resource settings (detected when omitted)
            db_name: File name of the cache database inside cache_dir
        """
        super().__init__(cache_dir, db_name, settings)
        self.data_dir = data_dir

    def check_table_ready(self) -> bool:
        """Check if cache tables exist and have data."""
        try:
            if not (
                self.table_exists("citing_aggregated")
                and self.table_exists("citation_details")
            ):
                return False
            agg_count = self.row_count("citing_aggregated")
            detail_count = self.row_count("citation_details")
            print("[OK] Citation cache tables ready")
            print(f"  citing_aggregated: {agg_count:,} rows")
            print(f"  citation_details: {detail_count:,} rows")
            return agg_count > 0 and detail_count > 0
        except Exception:
            return False

    # ==================== Build ====================

    def create_optimized_cache_table(
        self,
        force_recreate: bool = False,
        id_batch_size: int = DEFAULT_ID_BATCH_SIZE,
        create_indexes: bool = True,
        detail_partitions: int = DEFAULT_DETAIL_PARTITIONS,
    ) -> bool:
        """
        Create the citation cache tables from parquet files.

        Args:
            force_recreate: Rebuild even if the tables already exist
            id_batch_size: citingcorpusid range aggregated per batch
            create_indexes: Create ART indexes on the corpus ID columns
            detail_partitions: citingcorpusid ranges citation_details is built in

        Returns:
            True if successful
        """
        print("=== Building citation cache ===")

        if (
            self.table_exists("citing_aggregated")
            and self.table_exists("citation_details")
            and not force_recreate
        ):
            print(
                f"Existing cache found: citing_aggregated "
                f"{self.row_count('citing_aggregated'):,} rows, citation_details "
                f"{self.row_count('citation_details'):,} rows"
            )
            print("Using existing cache (pass force_recreate=True to rebuild)")
            return True

        parquet_files = list_parquet_files(self.data_dir)
        if not parquet_files:
            raise FileNotFoundError(f"No parquet files found in: {self.data_dir}")
        total_size = sum(f.stat().st_size for f in parquet_files)
        print(
            f"Found {len(parquet_files)} parquet files, "
            f"{total_size / (1024**3):.2f} GB"
        )

        start_time = time.time()

        print("\nStep 1: Deduplicate, filter and sort into citation_details...")
        self._create_detail_table(detail_partitions)

        print("\nStep 2: Aggregate per citing paper into citing_aggregated...")
        self._create_aggregated_table(id_batch_size)

        if create_indexes:
            print("\nStep 3: Create indexes...")
            step_start = time.time()
            self.con.execute(
                "CREATE INDEX IF NOT EXISTS idx_citingcorpusid ON citing_aggregated(citingcorpusid)"
            )
            self.con.execute(
                "CREATE INDEX IF NOT EXISTS idx_citation_count ON citing_aggregated(citation_count)"
            )
            self.con.execute(
                "CREATE INDEX IF NOT EXISTS idx_detail_citing ON citation_details(citingcorpusid)"
            )
            self.con.execute(
                "CREATE INDEX IF NOT EXISTS idx_detail_cited ON citation_details(citedcorpusid)"
            )
            print(f"Indexes created in {time.time() - step_start:.0f}s")

        self.con.execute("CHECKPOINT")
        print(f"\n=== Citation cache built in {time.time() - start_time:.0f}s ===")
        return True

    def _create_detail_table(self, partitions: int):
        step_start = time.time()
        source = (
            f"read_parquet({sql_literal(Path(self.data_dir) / '*.parquet')}, "
            "filename = true, union_by_name = true)"
        )

        # Rows without contexts are never cached, so drop them before
        # deduplicating; "newest version with contexts" is then simply the
        # newest remaining row.
        with_contexts = (
            "contexts IS NOT NULL AND ARRAY_LENGTH(contexts) > 0 "
            "AND contexts != ARRAY['null']"
        )

        self.con.execute(f"""
            CREATE OR REPLACE TABLE citation_details AS
            SELECT citingcorpusid, citedcorpusid, isinfluential,
                   contexts, intents, citationid
            FROM {source}
            LIMIT 0
        """)
        min_id, max_id = self.con.execute(
            f"SELECT MIN(citingcorpusid), MAX(citingcorpusid) FROM {source}"
        ).fetchone()
        deleted = create_latest_deletes_table(
            self.con, "deleted_citations", deletes_dir_for(self.data_dir), "citationid", "BIGINT",
            first_export_only=True,
        )
        print(f"citationids in S2 delete files: {deleted:,}")

        # All versions of a citation belong to the same citing paper, so
        # deduplicating within a citingcorpusid range is enough. Ranges are
        # inserted in order, which keeps the table sorted by citingcorpusid.
        ranges = []
        if min_id is not None:
            width = (max_id - min_id) // partitions + 1
            ranges = [(min_id + i * width, min_id + (i + 1) * width - 1) for i in range(partitions)]
        print(f"Building in {len(ranges)} citingcorpusid ranges (+ rows without citing ID)")

        for num, id_range in enumerate(ranges + [None], start=1):
            if id_range is None:
                where, params = "citingcorpusid IS NULL", []
            else:
                where, params = "citingcorpusid BETWEEN ? AND ?", list(id_range)
            self.con.execute(
                f"""
                INSERT INTO citation_details
                SELECT c.citingcorpusid, c.citedcorpusid, c.isinfluential,
                       c.contexts, c.intents, c.citationid
                FROM (
                    SELECT *, {FILE_BASENAME} AS version_file
                    FROM {source}
                    WHERE {where} AND {with_contexts}
                    QUALIFY citationid IS NULL
                         OR ROW_NUMBER() OVER (PARTITION BY citationid ORDER BY filename DESC) = 1
                ) c
                LEFT JOIN deleted_citations d ON c.citationid = d.citationid
                WHERE d.deleted_in IS NULL OR d.deleted_in < c.version_file
                ORDER BY c.citingcorpusid, c.citedcorpusid
                """,
                params,
            )
            print(f"  range {num}/{len(ranges) + 1} done ({time.time() - step_start:.0f}s)")
        self.con.execute("DROP TABLE IF EXISTS deleted_citations")

        detail_count = self.row_count("citation_details")
        leftover = self.con.execute(
            "SELECT COUNT(citationid) - COUNT(DISTINCT citationid) FROM citation_details"
        ).fetchone()[0]
        print(
            f"citation_details: {detail_count:,} rows with contexts, "
            f"{leftover:,} duplicate citationids ({time.time() - step_start:.0f}s)"
        )

    def _create_aggregated_table(self, id_batch_size: int):
        step_start = time.time()
        self.con.execute("""
            CREATE OR REPLACE TABLE citing_aggregated (
                citingcorpusid BIGINT,
                citation_count BIGINT,
                unique_cited_count BIGINT,
                influential_count BIGINT,
                all_cited_papers VARCHAR,
                all_contexts VARCHAR,
                all_intents VARCHAR
            )
        """)

        min_id, max_id = self.con.execute(
            "SELECT MIN(citingcorpusid), MAX(citingcorpusid) FROM citation_details"
        ).fetchone()
        if min_id is None:
            print("citation_details is empty, nothing to aggregate")
            return

        total_batches = (max_id - min_id) // id_batch_size + 1
        print(
            f"citingcorpusid range {min_id:,}..{max_id:,}: "
            f"{total_batches} batches of {id_batch_size:,} IDs"
        )

        # contexts/intents are lists; cast them explicitly (newer DuckDB versions
        # no longer cast lists to VARCHAR implicitly, older ones did exactly this).
        for batch_num, batch_start in enumerate(
            range(min_id, max_id + 1, id_batch_size), start=1
        ):
            batch_end = batch_start + id_batch_size - 1
            self.con.execute(
                """
                INSERT INTO citing_aggregated
                SELECT
                    citingcorpusid,
                    COUNT(*) AS citation_count,
                    COUNT(DISTINCT citedcorpusid) AS unique_cited_count,
                    SUM(CASE WHEN isinfluential = true THEN 1 ELSE 0 END) AS influential_count,
                    STRING_AGG(DISTINCT citedcorpusid::VARCHAR, ',') AS all_cited_papers,
                    STRING_AGG(DISTINCT contexts::VARCHAR, ' | ') AS all_contexts,
                    STRING_AGG(DISTINCT intents::VARCHAR, ' | ') AS all_intents
                FROM citation_details
                WHERE citingcorpusid BETWEEN ? AND ?
                GROUP BY citingcorpusid
                """,
                [batch_start, batch_end],
            )
            if batch_num % 10 == 0 or batch_num == total_batches:
                print(
                    f"  batch {batch_num}/{total_batches} done "
                    f"({time.time() - step_start:.0f}s)"
                )

        agg_count = self.row_count("citing_aggregated")
        print(
            f"citing_aggregated: {agg_count:,} rows ({time.time() - step_start:.0f}s)"
        )

    # ==================== Info ====================

    def show_cache_info(self):
        """Display cache statistics."""
        print("\n=== Citation cache info ===")
        try:
            total, avg_citations, max_citations, total_citations = self.con.execute("""
                SELECT COUNT(*), AVG(citation_count), MAX(citation_count), SUM(citation_count)
                FROM citing_aggregated
            """).fetchone()
            print(f"Citing papers: {total:,}")
            print(f"Avg citations with context per paper: {avg_citations:.2f}")
            print(f"Max citations with context per paper: {max_citations:,}")
            print(f"Total citations with context: {total_citations:,}")
            print(f"citation_details rows: {self.row_count('citation_details'):,}")
        except Exception as e:
            print(f"Error reading cache info: {e}")
