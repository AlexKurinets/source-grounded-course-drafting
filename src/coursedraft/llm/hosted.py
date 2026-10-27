"""Model-backed drafters. Each one returns the same pydantic objects as the offline drafter,
so the validators and the exporter never know which model wrote a draft.

These adapters are not exercised by the test suite, which runs offline. They are the shape of the
production adapters with the provider-specific retry and logging code left out.
"""

from __future__ import annotations

import json

import httpx
from pydantic import BaseModel, Field

from ..models import LAYOUT_WORD_CAPS, CaseExercise, Citation, FactRecord, GuideSection, Slide

SYSTEM = """You draft continuing-education course material for accountants from source excerpts.
Rules:
- Use only the numbered sources you are given. Do not add facts, figures or references.
- Cite a standards paragraph (e.g. NASB 116.22) only if it appears in that exact source, and
  return the citation as {"ref": ..., "chunk_id": ...} with that source's id.
- Participant-facing text may use only sources marked participant. Sources marked facilitator
  belong in the presenter notes.
- Stay within the word cap for the slide layout you are given."""


class OutlineItem(BaseModel):
    key: str
    kind: str
    layout: str
    title: str
    objective_id: str | None = None
    query: str


class Outline(BaseModel):
    items: list[OutlineItem]


class SlideDraft(BaseModel):
    title: str
    body: list[str] = Field(default_factory=list)
    right: list[str] = Field(default_factory=list)
    notes: str = ""
    citations: list[Citation] = Field(default_factory=list)
    source_chunk_ids: list[str] = Field(default_factory=list)


class Guide(BaseModel):
    sections: list[GuideSection]


def format_sources(chunks: list[dict]) -> str:
    return "\n\n".join(
        f"[{c['id']}] {c['audience']} | {c['source_type']} | {c['locator']}\n"
        f"{c['title']}\n{c['body']}"
        for c in chunks
    )


class LLMDrafter:
    adapter = "llm"

    def __init__(self, model: str) -> None:
        self.model = model

    def complete(self, user: str, schema: type[BaseModel]) -> BaseModel:
        raise NotImplementedError

    def plan(self, course: dict, objectives: list[dict]) -> list[dict]:
        user = (
            f"Course: {course['title']} ({course['duration_minutes']} minutes).\n"
            f"Objectives: {json.dumps(objectives)}\n"
            "Plan the deck: a title slide, a concept slide and an apply slide per objective, "
            f"and a closing slide. Allowed layouts: {list(LAYOUT_WORD_CAPS)}. Give each item a "
            "retrieval query."
        )
        return [i.model_dump() for i in self.complete(user, Outline).items]

    def draft_slide(self, item: dict, chunks: list[dict], ctx: dict) -> Slide:
        feedback = "\n".join(ctx.get("feedback", []))
        user = (
            f"Slide: {json.dumps(item)}\nWord cap: {LAYOUT_WORD_CAPS.get(item['layout'])}\n"
            + (
                f"The previous draft failed these checks, fix them:\n{feedback}\n"
                if feedback
                else ""
            )
            + f"Sources:\n{format_sources(chunks)}"
        )
        d = self.complete(user, SlideDraft)
        return Slide(
            key=item["key"],
            layout=item["layout"],
            objective_id=item.get("objective_id"),
            **d.model_dump(),
        )

    def draft_guide(
        self, course: dict, slides: list[Slide], facilitator: dict[str, list[dict]]
    ) -> list[GuideSection]:
        user = (
            f"Write a concise facilitator guide for {course['title']}: per objective, timing, "
            f"discussion flow, prompts, anticipated responses and likely misconceptions.\n"
            f"Objectives: {json.dumps(course['objectives'])}\n"
            f"Slides: {json.dumps([s.model_dump() for s in slides])}\n"
            f"Facilitator sources:\n"
            + format_sources([c for cs in facilitator.values() for c in cs])
        )
        return self.complete(user, Guide).sections

    def draft_case(self, course: dict, record: FactRecord, feedback: list[str]) -> CaseExercise:
        user = (
            "Write a participant case and a separate facilitator note from this fact record only. "
            "Every figure and name in either part must come from the record.\n"
            + (f"Fix these problems from the last draft: {feedback}\n" if feedback else "")
            + record.model_dump_json()
        )
        return self.complete(user, CaseExercise)


class AnthropicDrafter(LLMDrafter):
    adapter = "anthropic"

    def __init__(self, model: str) -> None:
        super().__init__(model)
        import anthropic

        self._client = anthropic.Anthropic()

    def complete(self, user: str, schema: type[BaseModel]) -> BaseModel:
        response = self._client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"model declined the request: {response.stop_details}")
        return response.parsed_output


class OpenAICompatibleDrafter(LLMDrafter):
    """Any OpenAI-compatible chat endpoint. Pointed at a local Ollama server, this is the route
    for restricted material, which never leaves the machine."""

    def __init__(
        self, model: str, base_url: str, api_key: str = "unused", adapter: str = "openai"
    ) -> None:
        super().__init__(model)
        self.adapter = adapter
        self._client = httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=300
        )

    def complete(self, user: str, schema: type[BaseModel]) -> BaseModel:
        prompt = f"{user}\n\nReply with JSON matching this schema:\n{schema.model_json_schema()}"
        r = self._client.post(
            "/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            },
        )
        r.raise_for_status()
        return schema.model_validate_json(r.json()["choices"][0]["message"]["content"])
