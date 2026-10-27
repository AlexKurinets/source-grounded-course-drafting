"""A deterministic drafter that assembles slides from the retrieved chunks.

It exists so the whole pipeline (retrieval, validation, redraft, approval, export) runs in tests
and in the demo stack without an API key. It writes plainer slides than a model would, but it
follows the same contract: participant text only from participant chunks, facilitator material
only in the notes, and every standards reference cited against the chunk it came from.
"""

from __future__ import annotations

import re

from ..models import LAYOUT_WORD_CAPS, CaseExercise, Citation, FactRecord, GuideSection, Slide
from ..text import REF_RE, sentences, stable_hash, trim_words

ANTICIPATED = re.compile(r"\b(expect|anticipated|most groups|often ask|will ask)\b", re.I)
MISCONCEPTION = re.compile(r"\b(mistake|assume|confuse|wrong|misread)\b", re.I)


def near_miss(ref: str) -> str:
    """NASB 116.22 -> NASB 116.23: the kind of plausible paragraph number models invent."""
    head, num = ref.rsplit(".", 1)
    return f"{head}.{int(num) + 1}"


def first_sentence(chunk: dict, limit: int = 20) -> str:
    body = REF_RE.sub("", chunk["body"]).replace("\n", " ").strip()
    parts = sentences(body)
    return trim_words(parts[0] if parts else body, limit)


class OfflineDrafter:
    adapter = "offline"

    def __init__(self, model: str = "offline-drafter", fault_every: int = 0) -> None:
        self.model = model
        self.fault_every = fault_every

    # -- planning ---------------------------------------------------------------------------
    def plan(self, course: dict, objectives: list[dict]) -> list[dict]:
        items = [
            {
                "key": "title",
                "kind": "title",
                "layout": "Title Slide",
                "title": course["title"],
                "objective_id": None,
                "query": course["title"],
            }
        ]
        for obj in objectives:
            items.append(
                {
                    "key": f"{obj['id']}-concept",
                    "kind": "concept",
                    "layout": "Title and Content",
                    "title": obj["short"],
                    "objective_id": obj["id"],
                    "query": obj["text"],
                }
            )
            items.append(
                {
                    "key": f"{obj['id']}-apply",
                    "kind": "apply",
                    "layout": "Two Content",
                    "title": f"Apply it: {obj['short'].lower()}",
                    "objective_id": obj["id"],
                    "query": f"{obj['text']} discuss example",
                }
            )
        items.append(
            {
                "key": "close",
                "kind": "close",
                "layout": "Title and Content",
                "title": "Key takeaways",
                "objective_id": None,
                "query": " ".join(o["text"] for o in objectives),
            }
        )
        return items

    # -- slides -----------------------------------------------------------------------------
    def draft_slide(self, item: dict, chunks: list[dict], ctx: dict) -> Slide:
        kind = item["kind"]
        slide = Slide(
            key=item["key"],
            layout=item["layout"],
            title=item["title"],
            objective_id=item.get("objective_id"),
        )
        participant = [c for c in chunks if c["audience"] == "participant"]
        facilitator = [c for c in chunks if c["audience"] == "facilitator"]
        if kind == "title":
            course = ctx["course"]
            slide.body = [
                f"{course['duration_minutes']} minutes",
                f"{len(course['objectives'])} learning objectives",
            ]
            slide.notes = "Open with the objectives and the case the group will work on."
            return slide
        if kind == "close":
            slide.body = [o["short"] for o in ctx["course"]["objectives"]]
            slide.body.append("Questions and wrap-up")
            slide.notes = "Ask each table for one change they will make in their next close."
            return slide

        used: list[str] = []
        if kind == "concept":
            # Explanations, not exercises: chunks that ask a question are discussion slides.
            explain = [c for c in participant if "?" not in c["body"]]
            slide.body = self._bullets(explain, slide, used, limit=4)
        else:
            scenario, questions = self._discussion(participant, item, used)
            standards = [c for c in participant if c["source_type"] == "standard"]
            slide.body = scenario + self._bullets(standards, slide, used, limit=1)
            slide.right = questions or [
                f"Where would {item['title'].split(': ', 1)[-1]} change a number you report?"
            ]
        slide.notes = self._notes(facilitator, used)
        slide.source_chunk_ids = used
        self._fit(slide)
        self._maybe_fault(slide, ctx)
        return slide

    def _bullets(self, chunks: list[dict], slide: Slide, used: list[str], limit: int) -> list[str]:
        bullets: list[str] = []
        for c in chunks:
            if len(bullets) >= limit:
                break
            if " | " in c["body"]:  # a table: useful context, not a bullet
                continue
            text = first_sentence(c)
            if not text or text in bullets or text.endswith("?"):
                continue
            if c["source_type"] == "standard" and c["paragraph_refs"]:
                ref = c["paragraph_refs"][0]
                text = f"{text} ({ref})"
                slide.citations.append(Citation(ref=ref, chunk_id=c["id"]))
            bullets.append(text)
            used.append(c["id"])
        return bullets

    def _discussion(self, chunks: list[dict], item: dict, used: list[str]):
        """The instructor's own discussion slide for this objective: its scenario goes on the
        left, its questions on the right."""
        disc = [c for c in chunks if "?" in c["body"] and " | " not in c["body"]]
        disc.sort(key=lambda c: item.get("objective_id") not in c["objective_ids"])
        if not disc:
            return [], []
        c = disc[0]
        used.append(c["id"])
        said = sentences(c["body"].replace("\n", " "))
        return (
            [s for s in said if not s.endswith("?")][:2],
            [s for s in said if s.endswith("?")][:2],
        )

    def _notes(self, facilitator: list[dict], used: list[str]) -> str:
        lines: list[str] = []
        for c in facilitator[:2]:
            prefix = "From practice: " if c["source_type"] == "client_notes" else ""
            lines.append(prefix + " ".join(sentences(c["body"].replace("\n", " "))[:2]))
            used.append(c["id"])
        return "\n".join(lines)

    def _fit(self, slide: Slide) -> None:
        cap = LAYOUT_WORD_CAPS.get(slide.layout, 70)
        while slide.on_slide_words() > cap and len(slide.body) > 1:
            dropped = slide.body.pop()
            slide.citations = [c for c in slide.citations if c.ref not in dropped]

    def _maybe_fault(self, slide: Slide, ctx: dict) -> None:
        if not self.fault_every or not slide.citations:
            return
        if stable_hash(f"{ctx['run_id']}|{slide.key}|{ctx['attempt']}") % self.fault_every:
            return
        good = slide.citations[0].ref
        bad = near_miss(good)
        slide.citations[0] = Citation(ref=bad, chunk_id=slide.citations[0].chunk_id)
        slide.body = [b.replace(f"({good})", f"({bad})") for b in slide.body]

    # -- facilitator guide ------------------------------------------------------------------
    def draft_guide(
        self, course: dict, slides: list[Slide], facilitator: dict[str, list[dict]]
    ) -> list[GuideSection]:
        objectives = course["objectives"]
        per_obj = max(10, (course["duration_minutes"] - 20) // len(objectives) // 5 * 5)
        sections = []
        for obj in objectives:
            mine = [s for s in slides if s.objective_id == obj["id"]]
            concept = next((s for s in mine if s.key.endswith("-concept")), None)
            apply = next((s for s in mine if s.key.endswith("-apply")), None)
            explain = per_obj * 2 // 5
            flow = []
            if concept:
                flow.append(f"Explain ({explain} min): {concept.title}")
            if apply:
                flow.append(f"Discuss in tables ({per_obj - explain - 5} min): {apply.title}")
            flow.append("Debrief (5 min): one table states the rule, another challenges it")
            chunks = facilitator.get(obj["id"], [])
            # Notes tagged with this objective first; retrieval also returns neighbours.
            chunks = [c for c in chunks if obj["id"] in c["objective_ids"]] or chunks
            said = [s for c in chunks for s in sentences(c["body"].replace("\n", " "))]
            anticipated = [s for s in said if ANTICIPATED.search(s)][:3]
            misconceptions = [s for s in said if MISCONCEPTION.search(s)][:3]
            sections.append(
                GuideSection(
                    objective_id=obj["id"],
                    title=obj["short"],
                    minutes=per_obj,
                    flow=flow,
                    prompts=apply.right if apply else [],
                    anticipated=anticipated
                    or ["None recorded in the source notes for this objective."],
                    misconceptions=misconceptions or ["None recorded in the source notes."],
                    source_chunk_ids=[c["id"] for c in chunks],
                )
            )
        return sections

    # -- case exercise ----------------------------------------------------------------------
    def draft_case(self, course: dict, record: FactRecord, feedback: list[str]) -> CaseExercise:
        questions = [f"What does {record.company} need to decide, and by when?"]
        questions += [
            f"How does {o['short'].lower()} bear on the decision?" for o in course["objectives"][:2]
        ]
        figures = "; ".join(f"{k}: {v}" for k, v in record.figures.items())
        return CaseExercise(
            title=f"{record.company}: {course['title']} case",
            participant=[
                f"{record.company} is a {record.industry}.",
                *record.facts,
                "Timeline: " + "; ".join(record.timeline) + ".",
            ],
            decision_point=record.decision_point,
            questions=questions,
            facilitator_note=[
                f"Figures to work from: {figures}.",
                f"Push each table for a recommendation to {record.decision_maker}, not a summary.",
                "Keep the debrief on the facts in the case; do not add new figures.",
            ],
        )
