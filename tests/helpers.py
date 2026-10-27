from __future__ import annotations

from coursedraft.ingest import ingest_file, put_fact_record, upsert_course
from coursedraft.llm.offline import OfflineDrafter, near_miss
from coursedraft.models import Citation
from coursedraft.runtime import Runtime
from coursedraft.synthetic import _course_meta, write_course_files


def ingest_course(rt: Runtime, course: dict) -> list:
    upsert_course(rt.pool, _course_meta(course))
    put_fact_record(rt.pool, course["id"], course["fact_record"])
    out = []
    for path, source_type in write_course_files(course, rt.settings.sources_dir):
        out.append(ingest_file(rt.pool, rt.embedder, rt.clock, course["id"], path, source_type))
    return out


class FaultyDrafter(OfflineDrafter):
    """Cites a near-miss paragraph number on the given slides and attempts, the way a model did
    in production."""

    def __init__(self, keys: set[str], attempts: set[int]) -> None:
        super().__init__(fault_every=0)
        self.keys = keys
        self.attempts = attempts

    def draft_slide(self, item, chunks, ctx):
        slide = super().draft_slide(item, chunks, ctx)
        if item["key"] in self.keys and ctx["attempt"] in self.attempts and slide.citations:
            good = slide.citations[0].ref
            bad = near_miss(good)
            slide.citations[0] = Citation(ref=bad, chunk_id=slide.citations[0].chunk_id)
            slide.body = [b.replace(f"({good})", f"({bad})") for b in slide.body]
        return slide
