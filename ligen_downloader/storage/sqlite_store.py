from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..domain import KeywordSet
from ..domain import ProviderRunStats
from ..domain import SearchRecord
from ..domain.models import utc_now_iso


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists keyword_sets (
                    id integer primary key autoincrement,
                    name text not null,
                    query_text text not null,
                    include_terms_json text not null,
                    exclude_terms_json text not null,
                    filters_json text not null,
                    normalized_hash text not null,
                    status text not null,
                    version integer not null,
                    parent_id integer,
                    locked_at text,
                    created_at text not null,
                    updated_at text not null,
                    notes text not null default ''
                );

                create table if not exists search_runs (
                    id integer primary key autoincrement,
                    keyword_set_id integer not null,
                    keyword_set_hash text not null,
                    query_text text not null,
                    provider_ids_json text not null,
                    raw_total integer not null,
                    unique_count integer not null,
                    duplicate_count integer not null,
                    overlap_count integer not null,
                    download_candidate_count integer not null,
                    created_at text not null
                );

                create table if not exists provider_stats (
                    id integer primary key autoincrement,
                    search_run_id integer not null,
                    provider_id text not null,
                    display_name text not null,
                    reported_total_count integer,
                    returned_count integer not null,
                    doi_count integer not null,
                    download_candidate_count integer not null,
                    error_count integer not null,
                    status text not null,
                    elapsed_seconds real not null
                );

                create table if not exists search_records (
                    id integer primary key autoincrement,
                    search_run_id integer not null,
                    provider_id text not null,
                    provider_item_id text not null,
                    title text not null,
                    doi text not null,
                    url text not null,
                    authors_json text not null,
                    year text not null,
                    venue text not null,
                    abstract text not null,
                    raw_json text not null,
                    dedupe_key text not null,
                    created_at text not null
                );
                """
            )

    def save_keyword_set(self, keyword_set: KeywordSet) -> KeywordSet:
        now = utc_now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into keyword_sets (
                    name, query_text, include_terms_json, exclude_terms_json, filters_json,
                    normalized_hash, status, version, parent_id, locked_at, created_at, updated_at, notes
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    keyword_set.name,
                    keyword_set.query_text,
                    json.dumps(keyword_set.include_terms, ensure_ascii=False),
                    json.dumps(keyword_set.exclude_terms, ensure_ascii=False),
                    json.dumps(keyword_set.filters, ensure_ascii=False),
                    keyword_set.normalized_hash,
                    keyword_set.status,
                    keyword_set.version,
                    keyword_set.parent_id,
                    keyword_set.locked_at,
                    keyword_set.created_at or now,
                    now,
                    keyword_set.notes,
                ),
            )
            new_id = int(cursor.lastrowid)
        return self.get_keyword_set(new_id)

    def get_keyword_set(self, keyword_set_id: int) -> KeywordSet:
        with self.connect() as conn:
            row = conn.execute("select * from keyword_sets where id = ?", (keyword_set_id,)).fetchone()
        if row is None:
            raise KeyError(f"KeywordSet not found: {keyword_set_id}")
        return self._keyword_set_from_row(row)

    def lock_keyword_set(self, keyword_set_id: int) -> KeywordSet:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                "update keyword_sets set status = 'locked', locked_at = ?, updated_at = ? where id = ?",
                (now, now, keyword_set_id),
            )
        return self.get_keyword_set(keyword_set_id)

    def latest_keyword_sets(self, limit: int = 10) -> list[KeywordSet]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from keyword_sets order by id desc limit ?",
                (limit,),
            ).fetchall()
        return [self._keyword_set_from_row(row) for row in rows]

    def save_search_run(
        self,
        *,
        keyword_set: KeywordSet,
        provider_stats: list[ProviderRunStats],
        records: list[SearchRecord],
        duplicate_count: int,
        overlap_count: int,
    ) -> int:
        now = utc_now_iso()
        raw_total = sum(stat.returned_count for stat in provider_stats)
        download_candidate_count = sum(1 for record in records if record.doi or record.url)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into search_runs (
                    keyword_set_id, keyword_set_hash, query_text, provider_ids_json,
                    raw_total, unique_count, duplicate_count, overlap_count,
                    download_candidate_count, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    keyword_set.id,
                    keyword_set.normalized_hash,
                    keyword_set.query_text,
                    json.dumps([stat.provider_id for stat in provider_stats]),
                    raw_total,
                    len(records),
                    duplicate_count,
                    overlap_count,
                    download_candidate_count,
                    now,
                ),
            )
            run_id = int(cursor.lastrowid)
            for stat in provider_stats:
                conn.execute(
                    """
                    insert into provider_stats (
                        search_run_id, provider_id, display_name, reported_total_count,
                        returned_count, doi_count, download_candidate_count, error_count,
                        status, elapsed_seconds
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        stat.provider_id,
                        stat.display_name,
                        stat.reported_total_count,
                        stat.returned_count,
                        stat.doi_count,
                        stat.download_candidate_count,
                        stat.error_count,
                        stat.status,
                        stat.elapsed_seconds,
                    ),
                )
            for record in records:
                conn.execute(
                    """
                    insert into search_records (
                        search_run_id, provider_id, provider_item_id, title, doi, url,
                        authors_json, year, venue, abstract, raw_json, dedupe_key, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        record.provider_id,
                        record.provider_item_id,
                        record.title,
                        record.doi,
                        record.url,
                        json.dumps(record.authors, ensure_ascii=False),
                        record.year,
                        record.venue,
                        record.abstract,
                        json.dumps(record.raw, ensure_ascii=False),
                        _dedupe_key(record),
                        now,
                    ),
                )
        return run_id

    def _keyword_set_from_row(self, row: sqlite3.Row) -> KeywordSet:
        return KeywordSet(
            id=int(row["id"]),
            name=str(row["name"]),
            query_text=str(row["query_text"]),
            include_terms=json.loads(str(row["include_terms_json"])),
            exclude_terms=json.loads(str(row["exclude_terms_json"])),
            filters=json.loads(str(row["filters_json"])),
            normalized_hash=str(row["normalized_hash"]),
            status=str(row["status"]),
            version=int(row["version"]),
            parent_id=int(row["parent_id"]) if row["parent_id"] is not None else None,
            locked_at=str(row["locked_at"]) if row["locked_at"] else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            notes=str(row["notes"] or ""),
        )


def _dedupe_key(record: SearchRecord) -> str:
    doi = (record.doi or "").strip().lower()
    if doi:
        return f"doi:{doi}"
    title = " ".join((record.title or "").lower().split())
    if title:
        return f"title:{title}:{record.year}"
    return f"{record.provider_id}:{record.provider_item_id}"
