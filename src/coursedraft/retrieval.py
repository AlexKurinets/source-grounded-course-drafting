"""Hybrid retrieval: Postgres full-text rank and pgvector distance, fused with reciprocal rank
fusion, then weighted by source type so the instructors' own decks outrank the publisher's."""

from __future__ import annotations

import re

from psycopg_pool import ConnectionPool

from .db import vector_literal
from .embed import Embedder
from .text import STOPWORDS

RRF_K = 60
SOURCE_WEIGHTS = {"own_deck": 1.6, "standard": 1.4, "client_notes": 1.1, "publisher_deck": 1.0}
OBJECTIVE_BONUS = 1.5
CANDIDATES = 50

SEARCH_SQL = """
WITH q AS (SELECT %(qvec)s::vector AS v, to_tsquery('english', %(tsq)s) AS tq),
base AS (
    SELECT c.id, c.title, c.body, c.audience, c.kind, c.locator, c.paragraph_refs,
           c.objective_ids, c.embedding, c.tsv, s.source_type, s.sensitivity,
           s.path AS source_path, s.version AS source_version
    FROM chunks c JOIN sources s ON s.id = c.source_id
    WHERE c.course_id = %(course)s AND s.current
      AND (%(audience)s::text IS NULL OR c.audience = %(audience)s)
),
vec AS (
    SELECT b.id, row_number() OVER (ORDER BY b.embedding <=> q.v) AS r
    FROM base b, q ORDER BY b.embedding <=> q.v LIMIT %(n)s
),
lex AS (
    SELECT b.id, row_number() OVER (ORDER BY ts_rank_cd(b.tsv, q.tq) DESC) AS r
    FROM base b, q WHERE b.tsv @@ q.tq
    ORDER BY ts_rank_cd(b.tsv, q.tq) DESC LIMIT %(n)s
)
SELECT b.id, b.title, b.body, b.audience, b.kind, b.locator, b.paragraph_refs, b.objective_ids,
       b.source_type, b.sensitivity, b.source_path, b.source_version,
       vec.r AS vrank, lex.r AS lrank
FROM base b LEFT JOIN vec ON vec.id = b.id LEFT JOIN lex ON lex.id = b.id
WHERE vec.id IS NOT NULL OR lex.id IS NOT NULL
"""


def or_query(text: str) -> str:
    words = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) > 2 and w not in STOPWORDS and w not in words:
            words.append(w)
    return " | ".join(words) or "zzznomatch"


def score(row: dict, objective_id: str | None) -> float:
    s = 1.0 / (RRF_K + row["vrank"]) if row["vrank"] else 0.0
    s += 1.0 / (RRF_K + row["lrank"]) if row["lrank"] else 0.0
    s *= SOURCE_WEIGHTS.get(row["source_type"], 1.0)
    if objective_id and objective_id in row["objective_ids"]:
        s *= OBJECTIVE_BONUS
    return s


def search(
    pool: ConnectionPool,
    embedder: Embedder,
    course_id: str,
    query: str,
    k: int = 6,
    audience: str | None = None,
    objective_id: str | None = None,
) -> list[dict]:
    params = {
        "qvec": vector_literal(embedder.embed(query)),
        "tsq": or_query(query),
        "course": course_id,
        "audience": audience,
        "n": CANDIDATES,
    }
    with pool.connection() as conn:
        rows = conn.execute(SEARCH_SQL, params).fetchall()
    for r in rows:
        r["score"] = score(r, objective_id)
    rows.sort(key=lambda r: (-r["score"], r["id"]))
    return rows[:k]


def get_chunks(pool: ConnectionPool, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT c.id, c.course_id, c.title, c.body, c.audience, c.kind, c.locator,
                      c.paragraph_refs, c.objective_ids, s.source_type, s.sensitivity,
                      s.path AS source_path, s.version AS source_version,
                      s.current AS source_current
               FROM chunks c JOIN sources s ON s.id = c.source_id WHERE c.id = ANY(%s)""",
            (list(ids),),
        ).fetchall()
    return {r["id"]: r for r in rows}
