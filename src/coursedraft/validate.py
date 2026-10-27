"""Deterministic checks that run on every draft before a person sees it.

None of these asks a model whether the output is right. A second model as citation judge was tried
in production and missed near-miss paragraph numbers; the checks that stuck compare the draft with
the source register directly.
"""

from __future__ import annotations

import re

from psycopg_pool import ConnectionPool

from .models import LAYOUT_WORD_CAPS, Draft, FactRecord, Issue, Slide
from .retrieval import get_chunks
from .text import paragraph_refs, shingles

FIGURE_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?(?:\s?(?:million|billion|m|bn))?", re.I)
COMPANY_RE = re.compile(
    r"\b((?:[A-Z][a-z]+ ){1,3}(?:Inc|Ltd|LLC|Group|Services|Partners|Holdings|Co)\b\.?)"
)


def ref_index(pool: ConnectionPool, course_id: str) -> dict[str, list[str]]:
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT ref, array_agg(c.id ORDER BY c.id) AS ids
               FROM chunks c JOIN sources s ON s.id = c.source_id,
                    unnest(c.paragraph_refs) AS ref
               WHERE c.course_id = %s AND s.current GROUP BY ref""",
            (course_id,),
        ).fetchall()
    return {r["ref"]: r["ids"] for r in rows}


def facilitator_shingles(pool: ConnectionPool, course_id: str) -> set[str]:
    with pool.connection() as conn:
        rows = conn.execute(
            """SELECT c.body FROM chunks c JOIN sources s ON s.id = c.source_id
               WHERE c.course_id = %s AND s.current AND c.audience = 'facilitator'""",
            (course_id,),
        ).fetchall()
    out: set[str] = set()
    for r in rows:
        out |= shingles(r["body"])
    return out


def check_citations(slide: Slide, chunks: dict[str, dict], refs: dict[str, list[str]]) -> list:
    issues = []
    for c in slide.citations:
        chunk = chunks.get(c.chunk_id)
        if chunk is None:
            issues.append(
                Issue(
                    slide_key=slide.key,
                    check="citation",
                    detail=f"{c.ref} cites {c.chunk_id}, which is not in the source register",
                )
            )
        elif not chunk["source_current"]:
            issues.append(
                Issue(
                    slide_key=slide.key,
                    check="citation",
                    detail=f"{c.ref} cites {c.chunk_id} from a superseded version of "
                    f"{chunk['source_path']}",
                )
            )
        elif c.ref not in chunk["paragraph_refs"]:
            where = refs.get(c.ref)
            detail = f"{c.ref} is not in {c.chunk_id} ({chunk['source_path']}, {chunk['locator']})"
            detail += (
                f"; it appears in {', '.join(where)}"
                if where
                else "; no registered source contains it"
            )
            issues.append(Issue(slide_key=slide.key, check="citation", detail=detail))
    cited = {c.ref for c in slide.citations}
    for ref in paragraph_refs(" ".join(slide.body + slide.right)):
        if ref not in cited:
            issues.append(
                Issue(
                    slide_key=slide.key,
                    check="citation",
                    detail=f"{ref} appears on the slide without a citation",
                )
            )
    return issues


def check_audience(slide: Slide, fac: set[str]) -> list[Issue]:
    if slide.audience != "participant":
        return [
            Issue(
                slide_key=slide.key,
                check="audience",
                detail="facilitator-only slide in the participant deck",
            )
        ]
    leaked = shingles(" ".join([slide.title, *slide.body, *slide.right])) & fac
    if leaked:
        sample = sorted(leaked)[0]
        return [
            Issue(
                slide_key=slide.key,
                check="audience",
                detail=f'participant text repeats facilitator-only notes: "{sample}..."',
            )
        ]
    return []


def check_layout(slide: Slide) -> list[Issue]:
    cap = LAYOUT_WORD_CAPS.get(slide.layout)
    if cap is None:
        return [
            Issue(
                slide_key=slide.key,
                check="layout",
                detail=f"layout {slide.layout!r} is not one of the mapped layouts",
            )
        ]
    words = slide.on_slide_words()
    if words > cap:
        return [
            Issue(
                slide_key=slide.key,
                check="layout",
                detail=f"{words} words on a {slide.layout} slide (cap {cap})",
            )
        ]
    return []


def _norm_figure(f: str) -> str:
    return re.sub(r"[\s$,]", "", f.lower())


def check_case(draft: Draft, record: FactRecord | None) -> list[Issue]:
    """Every figure and company name in the case, participant side or facilitator side, must be
    in the fact record. Single-digit counts are left alone; they are usually question numbers."""
    if draft.case is None or record is None:
        return []
    known_text = record.text()
    known = {_norm_figure(f) for f in FIGURE_RE.findall(known_text)}
    case = draft.case
    issues = []
    parts = {
        "participant": " ".join(case.participant + [case.decision_point] + case.questions),
        "facilitator note": " ".join(case.facilitator_note),
    }
    for where, text in parts.items():
        for fig in FIGURE_RE.findall(text):
            n = _norm_figure(fig)
            if len(re.sub(r"\D", "", n)) < 2 and "$" not in fig and "%" not in fig:
                continue
            if n not in known:
                issues.append(
                    Issue(
                        slide_key="case",
                        check="fact_record",
                        detail=f"{where} uses {fig.strip()}, which is not in the fact record",
                    )
                )
        for name in COMPANY_RE.findall(text):
            if name.rstrip(".") not in known_text:
                issues.append(
                    Issue(
                        slide_key="case",
                        check="fact_record",
                        detail=f"{where} names {name}, which is not in the fact record",
                    )
                )
    return issues


def validate_draft(
    pool: ConnectionPool, course_id: str, draft: Draft, record: FactRecord | None
) -> list[Issue]:
    cited_ids = [c.chunk_id for s in draft.slides for c in s.citations]
    chunks = get_chunks(pool, cited_ids)
    refs = ref_index(pool, course_id)
    fac = facilitator_shingles(pool, course_id)
    issues: list[Issue] = []
    for slide in draft.slides:
        issues += check_citations(slide, chunks, refs)
        issues += check_audience(slide, fac)
        issues += check_layout(slide)
    issues += check_case(draft, record)
    return issues
