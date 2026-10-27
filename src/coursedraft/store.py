"""Queries over runs, draft versions, approvals, exports and the model-call audit log."""

from __future__ import annotations

import json
from datetime import datetime

from psycopg_pool import ConnectionPool


class NotFound(Exception):
    pass


def get_course(pool: ConnectionPool, course_id: str) -> dict:
    with pool.connection() as conn:
        row = conn.execute("SELECT * FROM courses WHERE id = %s", (course_id,)).fetchone()
    if row is None:
        raise NotFound(f"course {course_id}")
    return row


def list_courses(pool: ConnectionPool) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute("SELECT * FROM courses ORDER BY title").fetchall()


def get_fact_record(pool: ConnectionPool, course_id: str) -> dict | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT record FROM fact_records WHERE course_id = %s", (course_id,)
        ).fetchone()
    return row["record"] if row else None


def next_run_id(pool: ConnectionPool, course_id: str, kind: str = "course") -> str:
    prefix = "eval-" if kind == "eval" else ""
    with pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM runs WHERE course_id = %s AND kind = %s", (course_id, kind)
        ).fetchone()["n"]
    return f"{prefix}{course_id}-r{n + 1}"


def create_run(pool: ConnectionPool, run_id: str, course_id: str, kind: str, now: datetime) -> None:
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO runs (id, course_id, kind, status, created_at, updated_at)
               VALUES (%s, %s, %s, 'drafting', %s, %s)""",
            (run_id, course_id, kind, now, now),
        )


def set_status(pool: ConnectionPool, run_id: str, status: str, now: datetime) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE runs SET status = %s, updated_at = %s WHERE id = %s", (status, now, run_id)
        )


def save_draft(
    pool: ConnectionPool, run_id: str, draft: dict, validation: dict, now: datetime
) -> dict:
    with pool.connection() as conn, conn.transaction():
        version = conn.execute(
            "SELECT coalesce(max(version), 0) + 1 AS v FROM draft_versions WHERE run_id = %s",
            (run_id,),
        ).fetchone()["v"]
        row = conn.execute(
            """INSERT INTO draft_versions (run_id, version, draft, validation, created_at)
               VALUES (%s, %s, %s, %s, %s) RETURNING id, version""",
            (run_id, version, json.dumps(draft), json.dumps(validation), now),
        ).fetchone()
        conn.execute(
            "UPDATE runs SET status = 'awaiting_approval', updated_at = %s WHERE id = %s",
            (now, run_id),
        )
    return row


def record_decision(
    pool: ConnectionPool,
    draft_version_id: int,
    decision: str,
    reviewer: str,
    notes: str,
    source_chunk_ids: list[str],
    now: datetime,
) -> int:
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            """INSERT INTO approvals (draft_version_id, decision, reviewer, notes,
                                      source_chunk_ids, decided_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (draft_version_id) DO NOTHING RETURNING id""",
            (draft_version_id, decision, reviewer, notes, source_chunk_ids, now),
        ).fetchone()
        if row is None:  # the gate node re-ran after a crash; keep the first decision
            row = conn.execute(
                "SELECT id FROM approvals WHERE draft_version_id = %s", (draft_version_id,)
            ).fetchone()
        if decision == "returned":
            conn.execute(
                """UPDATE runs SET status = 'returned', updated_at = %s
                   WHERE id = (SELECT run_id FROM draft_versions WHERE id = %s)""",
                (now, draft_version_id),
            )
    return row["id"]


def get_approval(pool: ConnectionPool, draft_version_id: int) -> dict | None:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM approvals WHERE draft_version_id = %s", (draft_version_id,)
        ).fetchone()


def get_draft_version(pool: ConnectionPool, draft_version_id: int) -> dict:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM draft_versions WHERE id = %s", (draft_version_id,)
        ).fetchone()
    if row is None:
        raise NotFound(f"draft version {draft_version_id}")
    return row


def record_export(
    pool: ConnectionPool, approval_id: int, kind: str, path: str, sha256: str, now: datetime
) -> None:
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO exports (approval_id, kind, path, sha256, created_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (approval_id, kind, path, sha256, now),
        )


def log_model_call(pool: ConnectionPool, **row) -> None:
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO model_calls (run_id, step, route, adapter, model, sensitivity,
                                        chunk_ids, chars_sent, created_at)
               VALUES (%(run_id)s, %(step)s, %(route)s, %(adapter)s, %(model)s, %(sensitivity)s,
                       %(chunk_ids)s, %(chars_sent)s, %(created_at)s)""",
            row,
        )


RUN_SUMMARY_SQL = """
SELECT r.*, c.title AS course_title, d.id AS draft_version_id, d.version,
       jsonb_array_length(d.draft -> 'slides') AS slides,
       d.validation, a.decision, a.reviewer, a.notes AS review_notes, a.decided_at
FROM runs r
JOIN courses c ON c.id = r.course_id
LEFT JOIN LATERAL (
    SELECT * FROM draft_versions dv WHERE dv.run_id = r.id ORDER BY dv.version DESC LIMIT 1
) d ON TRUE
LEFT JOIN approvals a ON a.draft_version_id = d.id
"""


def list_runs(pool: ConnectionPool, kind: str = "course", status: str | None = None) -> list[dict]:
    sql = RUN_SUMMARY_SQL + " WHERE r.kind = %s AND (%s::text IS NULL OR r.status = %s)"
    sql += " ORDER BY r.created_at DESC, r.id DESC"
    with pool.connection() as conn:
        return conn.execute(sql, (kind, status, status)).fetchall()


def get_run(pool: ConnectionPool, run_id: str) -> dict:
    with pool.connection() as conn:
        row = conn.execute(RUN_SUMMARY_SQL + " WHERE r.id = %s", (run_id,)).fetchone()
        if row is None:
            raise NotFound(f"run {run_id}")
        if row["draft_version_id"]:
            row["draft"] = conn.execute(
                "SELECT draft FROM draft_versions WHERE id = %s", (row["draft_version_id"],)
            ).fetchone()["draft"]
            row["exports"] = conn.execute(
                """SELECT e.id, e.kind, e.path, e.sha256, e.created_at FROM exports e
                   JOIN approvals a ON a.id = e.approval_id
                   WHERE a.draft_version_id = %s ORDER BY e.id""",
                (row["draft_version_id"],),
            ).fetchall()
    return row


def last_approved(pool: ConnectionPool, course_id: str, before_run: str) -> dict | None:
    """The most recent approved draft for the course from another run, for side-by-side review."""
    with pool.connection() as conn:
        return conn.execute(
            """SELECT r.id AS run_id, d.version, d.draft, a.reviewer, a.decided_at
               FROM approvals a JOIN draft_versions d ON d.id = a.draft_version_id
               JOIN runs r ON r.id = d.run_id
               WHERE r.course_id = %s AND r.kind = 'course' AND a.decision = 'approved'
                 AND r.id <> %s
               ORDER BY a.decided_at DESC LIMIT 1""",
            (course_id, before_run),
        ).fetchone()


def get_export(pool: ConnectionPool, export_id: int) -> dict:
    with pool.connection() as conn:
        row = conn.execute("SELECT * FROM exports WHERE id = %s", (export_id,)).fetchone()
    if row is None:
        raise NotFound(f"export {export_id}")
    return row


def list_sources(pool: ConnectionPool) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(
            """SELECT s.*, c.title AS course_title, count(ch.id) AS chunks
               FROM sources s JOIN courses c ON c.id = s.course_id
               LEFT JOIN chunks ch ON ch.source_id = s.id
               GROUP BY s.id, c.title ORDER BY s.course_id, s.path, s.version"""
        ).fetchall()


def list_model_calls(pool: ConnectionPool, run_id: str | None = None, limit: int = 200) -> list:
    with pool.connection() as conn:
        return conn.execute(
            """SELECT m.* FROM model_calls m JOIN runs r ON r.id = m.run_id
               WHERE r.kind = 'course' AND (%s::text IS NULL OR m.run_id = %s)
               ORDER BY m.id DESC LIMIT %s""",
            (run_id, run_id, limit),
        ).fetchall()


def stats(pool: ConnectionPool) -> dict:
    with pool.connection() as conn:
        by_status = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, count(*) AS n FROM runs WHERE kind = 'course' GROUP BY status"
            ).fetchall()
        }
        v = conn.execute(
            """SELECT coalesce(sum((d.validation ->> 'caught')::int), 0) AS caught,
                      coalesce(sum((d.validation ->> 'citation_caught')::int), 0) AS cit_caught,
                      coalesce(sum((d.validation ->> 'flagged')::int), 0) AS flagged,
                      coalesce(sum(jsonb_array_length(d.draft -> 'slides')), 0) AS slides
               FROM draft_versions d JOIN runs r ON r.id = d.run_id WHERE r.kind = 'course'"""
        ).fetchone()
        calls = conn.execute(
            """SELECT m.route, count(*) AS n FROM model_calls m JOIN runs r ON r.id = m.run_id
               WHERE r.kind = 'course' GROUP BY m.route"""
        ).fetchall()
    return {
        "runs_by_status": by_status,
        "slides_drafted": v["slides"],
        "issues_caught": v["caught"],
        "citation_faults_caught": v["cit_caught"],
        "slides_flagged": v["flagged"],
        "model_calls_by_route": {r["route"]: r["n"] for r in calls},
    }
