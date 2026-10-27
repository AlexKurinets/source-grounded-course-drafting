from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

Audience = Literal["participant", "facilitator"]

# The six layouts the exporter maps by name from the slide master, with a word cap for each.
# Anything else, or anything over the cap, fails validation instead of producing a cramped slide.
LAYOUT_WORD_CAPS: dict[str, int] = {
    "Title Slide": 25,
    "Section Header": 20,
    "Title and Content": 70,
    "Two Content": 80,
    "Comparison": 90,
    "Title Only": 30,
}


def word_count(*parts: str) -> int:
    return sum(len(re.findall(r"\S+", p)) for p in parts)


class Citation(BaseModel):
    ref: str
    chunk_id: str


class Slide(BaseModel):
    key: str
    layout: str
    title: str
    body: list[str] = Field(default_factory=list)
    right: list[str] = Field(default_factory=list)
    notes: str = ""
    audience: Audience = "participant"
    objective_id: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    source_chunk_ids: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    attempts: int = 1

    def on_slide_words(self) -> int:
        return word_count(self.title, *self.body, *self.right)


class GuideSection(BaseModel):
    objective_id: str
    title: str
    minutes: int
    flow: list[str]
    prompts: list[str]
    anticipated: list[str]
    misconceptions: list[str]
    source_chunk_ids: list[str] = Field(default_factory=list)


class FactRecord(BaseModel):
    company: str
    industry: str
    decision_maker: str
    decision_point: str
    timeline: list[str]
    figures: dict[str, str]
    facts: list[str]

    def text(self) -> str:
        return " ".join(
            [self.company, self.industry, self.decision_maker, self.decision_point]
            + self.timeline
            + [f"{k} {v}" for k, v in self.figures.items()]
            + self.facts
        )


class CaseExercise(BaseModel):
    title: str
    participant: list[str]
    decision_point: str
    questions: list[str]
    facilitator_note: list[str]


class Issue(BaseModel):
    slide_key: str
    check: Literal["citation", "audience", "layout", "fact_record"]
    detail: str


class Draft(BaseModel):
    slides: list[Slide]
    guide: list[GuideSection] = Field(default_factory=list)
    case: CaseExercise | None = None

    def chunk_ids(self) -> list[str]:
        ids: set[str] = set()
        for s in self.slides:
            ids.update(s.source_chunk_ids)
            ids.update(c.chunk_id for c in s.citations)
        for g in self.guide:
            ids.update(g.source_chunk_ids)
        return sorted(ids)
