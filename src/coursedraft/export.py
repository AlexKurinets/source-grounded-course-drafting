"""Write approved drafts into the provider's own PowerPoint and Word templates.

Nothing here runs without an approval record for the exact draft version, whoever calls it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from docx import Document
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

from . import store
from .models import Draft, Slide
from .retrieval import get_chunks
from .text import sha256_bytes

if TYPE_CHECKING:
    from .runtime import Runtime

DECK_TEMPLATE = "course-master.pptx"
DOC_TEMPLATE = "facilitator-guide.docx"


class NotApproved(Exception):
    pass


class ExportError(Exception):
    pass


def _fill(placeholder, lines: list[str]) -> None:
    tf = placeholder.text_frame
    tf.text = lines[0] if lines else ""
    for line in lines[1:]:
        tf.add_paragraph().text = line


def _source_line(cid: str, chunks: dict[str, dict]) -> str:
    c = chunks.get(cid)
    if c is None:
        return f"{cid} (missing)"
    refs = f" [{', '.join(c['paragraph_refs'])}]" if c["paragraph_refs"] else ""
    return f"{cid}: {c['source_path']} v{c['source_version']}, {c['locator']}{refs}"


def write_deck(
    template: Path, course: dict, draft: Draft, chunks: dict, approval: dict, path: Path
) -> None:
    prs = Presentation(str(template))
    layouts = {layout.name: layout for layout in prs.slide_layouts}
    for slide in draft.slides:
        if slide.audience != "participant":
            raise ExportError(f"slide {slide.key} is not participant-facing")
        layout = layouts.get(slide.layout)
        if layout is None:
            raise ExportError(f"layout {slide.layout!r} is not in {template.name}")
        s = prs.slides.add_slide(layout)
        _write_slide(s, slide)
        notes = [slide.notes] if slide.notes else []
        ids = sorted(set(slide.source_chunk_ids) | {c.chunk_id for c in slide.citations})
        if ids:
            notes.append("Sources:\n" + "\n".join(_source_line(i, chunks) for i in ids))
        s.notes_slide.notes_text_frame.text = "\n\n".join(notes)
    prs.core_properties.title = course["title"]
    prs.core_properties.comments = (
        f"Approved by {approval['reviewer']} on {approval['decided_at']:%Y-%m-%d}"
    )
    prs.save(str(path))


def _write_slide(s, slide: Slide) -> None:
    if s.shapes.title is not None:
        s.shapes.title.text = slide.title
    bodies = sorted(
        (ph for ph in s.placeholders if ph.placeholder_format.idx != 0),
        key=lambda ph: ph.placeholder_format.idx,
    )
    columns = [slide.body, slide.right] if slide.right else [slide.body]
    for ph in bodies:
        if columns and columns[0]:
            _fill(ph, columns.pop(0))
        else:
            ph._element.getparent().remove(ph._element)  # no empty "click to add" boxes
    if columns and columns[0]:  # a layout with fewer placeholders than columns, e.g. Title Only
        box = s.shapes.add_textbox(Inches(0.6), Inches(1.8), Inches(8.8), Inches(4.5))
        _fill(box, [line for col in columns for line in col])
    if slide.citations:
        box = s.shapes.add_textbox(Inches(0.5), Inches(6.9), Inches(9), Inches(0.4))
        p = box.text_frame.paragraphs[0]
        p.text = "Source: " + "; ".join(dict.fromkeys(c.ref for c in slide.citations))
        p.runs[0].font.size = Pt(10)
        p.runs[0].font.color.rgb = RGBColor(0x59, 0x59, 0x59)


def _bullets(doc, items: list[str], style: str = "List Bullet") -> None:
    for item in items:
        doc.add_paragraph(item, style=style)


def write_guide(
    template: Path,
    course: dict,
    draft: Draft,
    chunks: dict,
    approval: dict,
    version: int,
    path: Path,
) -> None:
    doc = Document(str(template))
    doc.add_heading(f"{course['title']}: facilitator guide", 0)
    doc.add_paragraph(
        f"Instructor-only. Draft version {version}, approved by {approval['reviewer']} on "
        f"{approval['decided_at']:%d %B %Y}. {course['duration_minutes']} minutes in total."
    )
    table = doc.add_table(rows=1, cols=3)
    table.style = "Light List Accent 1"
    for cell, text in zip(table.rows[0].cells, ("Objective", "Focus", "Minutes"), strict=True):
        cell.text = text
    for g in draft.guide:
        row = table.add_row().cells
        row[0].text, row[1].text, row[2].text = g.objective_id, g.title, str(g.minutes)
    for g in draft.guide:
        doc.add_heading(f"{g.objective_id}: {g.title} ({g.minutes} min)", 1)
        for label, items in (
            ("Discussion flow", g.flow),
            ("Prompts", g.prompts),
            ("Anticipated responses", g.anticipated),
            ("Likely misconceptions", g.misconceptions),
        ):
            doc.add_heading(label, 3)
            _bullets(doc, items)
    if draft.case:
        doc.add_heading(f"Case: {draft.case.title}", 1)
        doc.add_heading("Facilitator note", 3)
        _bullets(doc, draft.case.facilitator_note)
    doc.add_heading("Sources behind this guide and the deck", 1)
    _bullets(doc, [_source_line(i, chunks) for i in draft.chunk_ids()])
    doc.core_properties.title = f"{course['title']}: facilitator guide"
    doc.core_properties.comments = f"Approved by {approval['reviewer']}"
    doc.save(str(path))


def write_case(template: Path, draft: Draft, path: Path) -> None:
    case = draft.case
    doc = Document(str(template))
    doc.add_heading(case.title, 0)
    for para in case.participant:
        doc.add_paragraph(para)
    doc.add_heading("Decision point", 2)
    doc.add_paragraph(case.decision_point)
    doc.add_heading("Questions", 2)
    _bullets(doc, case.questions, style="List Number")
    doc.core_properties.title = case.title
    doc.save(str(path))


def export_approved(rt: Runtime, draft_version_id: int) -> list[dict]:
    approval = store.get_approval(rt.pool, draft_version_id)
    if approval is None or approval["decision"] != "approved":
        raise NotApproved(f"draft version {draft_version_id} has no approval record")
    dv = store.get_draft_version(rt.pool, draft_version_id)
    run = store.get_run(rt.pool, dv["run_id"])
    course = store.get_course(rt.pool, run["course_id"])
    draft = Draft.model_validate(dv["draft"])
    chunks = get_chunks(rt.pool, draft.chunk_ids())
    tdir = rt.settings.template_dir
    out = rt.settings.exports_dir / dv["run_id"] / f"v{dv['version']}"
    out.mkdir(parents=True, exist_ok=True)
    stem = course["id"]
    files = [("deck", out / f"{stem}-deck.pptx"), ("guide", out / f"{stem}-facilitator-guide.docx")]
    write_deck(tdir / DECK_TEMPLATE, course, draft, chunks, approval, files[0][1])
    write_guide(tdir / DOC_TEMPLATE, course, draft, chunks, approval, dv["version"], files[1][1])
    if draft.case:
        files.append(("case", out / f"{stem}-case-participant.docx"))
        write_case(tdir / DOC_TEMPLATE, draft, files[2][1])
    now = rt.clock.now()
    written = []
    for kind, path in files:
        digest = sha256_bytes(path.read_bytes())
        store.record_export(rt.pool, approval["id"], kind, str(path), digest, now)
        written.append({"kind": kind, "path": str(path), "sha256": digest})
    store.set_status(rt.pool, dv["run_id"], "exported", now)
    return written
