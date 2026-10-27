"""The drafting workflow as a LangGraph state graph, checkpointed in Postgres.

plan -> retrieve -> draft -> validate -> (one redraft of failing items) -> save -> approval gate
-> export. The approval gate is an ``interrupt``: the run's state is written to Postgres and the
process can exit. A later request, possibly from another process, resumes it with the decision.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import store
from .models import Draft, FactRecord
from .retrieval import search
from .validate import validate_draft

if TYPE_CHECKING:
    from .runtime import Runtime

MAX_ATTEMPTS = 2


class DraftState(TypedDict, total=False):
    run_id: str
    course_id: str
    outline: list[dict]
    retrieved: dict[str, list[dict]]
    slides: list[dict]
    guide: list[dict]
    case: dict | None
    attempts: dict[str, int]
    issues: list[dict]
    history: list[dict]
    draft_version_id: int
    version: int
    decision: dict


def _record(rt: Runtime, course_id: str) -> FactRecord | None:
    raw = store.get_fact_record(rt.pool, course_id)
    return FactRecord.model_validate(raw) if raw else None


def _retry_keys(issues: list[dict], attempts: dict[str, int]) -> list[str]:
    keys = {i["slide_key"] for i in issues}
    return sorted(k for k in keys if attempts.get(k, 0) < MAX_ATTEMPTS)


def build_graph(rt: Runtime):
    def plan(state: DraftState) -> dict:
        course = store.get_course(rt.pool, state["course_id"])
        outline = rt.router.call(
            state["run_id"], "plan", [], lambda m: m.plan(course, course["objectives"])
        )
        return {"outline": outline, "attempts": {}, "history": [], "issues": [], "slides": []}

    def retrieve(state: DraftState) -> dict:
        course = store.get_course(rt.pool, state["course_id"])
        found: dict[str, list[dict]] = {}
        for item in state["outline"]:
            if item["kind"] == "title":
                found[item["key"]] = []
            else:
                found[item["key"]] = search(
                    rt.pool,
                    rt.embedder,
                    state["course_id"],
                    item["query"],
                    k=8,
                    objective_id=item.get("objective_id"),
                )
        for obj in course["objectives"]:
            found[f"guide:{obj['id']}"] = search(
                rt.pool,
                rt.embedder,
                state["course_id"],
                obj["text"],
                k=4,
                audience="facilitator",
                objective_id=obj["id"],
            )
        return {"retrieved": found}

    def draft(state: DraftState) -> dict:
        run_id, course_id = state["run_id"], state["course_id"]
        course = store.get_course(rt.pool, course_id)
        attempts = dict(state.get("attempts") or {})
        issues = state.get("issues") or []
        feedback = defaultdict(list)
        for i in issues:
            feedback[i["slide_key"]].append(i["detail"])
        first = not state.get("slides")
        todo = None if first else set(_retry_keys(issues, attempts))
        slides = {s["key"]: s for s in state.get("slides") or []}
        for item in state["outline"]:
            key = item["key"]
            if todo is not None and key not in todo:
                continue
            attempts[key] = attempts.get(key, 0) + 1
            chunks = state["retrieved"][key]
            ctx = {
                "run_id": run_id,
                "attempt": attempts[key],
                "course": course,
                "feedback": feedback.get(key, []),
            }
            slide = rt.router.call(
                run_id,
                f"draft {key}",
                chunks,
                lambda m, i=item, c=chunks, x=ctx: m.draft_slide(i, c, x),
            )
            slide.attempts = attempts[key]
            slides[key] = slide.model_dump()
        ordered = [slides[i["key"]] for i in state["outline"]]

        out: dict[str, Any] = {"slides": ordered, "attempts": attempts}
        record = _record(rt, course_id)
        if record and (first or (todo and "case" in todo)):
            attempts["case"] = attempts.get("case", 0) + 1
            case = rt.router.call(
                run_id,
                "draft case",
                [],
                lambda m: m.draft_case(course, record, feedback.get("case", [])),
                sensitivity="internal",
                extra_ids=[f"fact_record:{course_id}"],
            )
            out["case"] = case.model_dump()

        fac = {o["id"]: state["retrieved"][f"guide:{o['id']}"] for o in course["objectives"]}
        fac_chunks = [c for cs in fac.values() for c in cs]
        draft_obj = Draft.model_validate({"slides": ordered})
        guide = rt.router.call(
            run_id,
            "draft guide",
            fac_chunks,
            lambda m: m.draft_guide(course, draft_obj.slides, fac),
        )
        out["guide"] = [g.model_dump() for g in guide]
        return out

    def validate(state: DraftState) -> dict:
        draft_obj = Draft.model_validate(
            {"slides": state["slides"], "guide": state["guide"], "case": state.get("case")}
        )
        issues = [
            i.model_dump()
            for i in validate_draft(
                rt.pool, state["course_id"], draft_obj, _record(rt, state["course_id"])
            )
        ]
        history = [
            *state.get("history", []),
            {"pass": len(state.get("history", [])) + 1, "issues": issues},
        ]
        out: dict[str, Any] = {"issues": issues, "history": history}
        if not _retry_keys(issues, state.get("attempts", {})):
            # Second failure: the item goes to the reviewer flagged, not silently patched.
            slides = []
            for s in state["slides"]:
                s = dict(s)
                s["flags"] = [i["detail"] for i in issues if i["slide_key"] == s["key"]]
                slides.append(s)
            out["slides"] = slides
        return out

    def after_validate(state: DraftState) -> str:
        return "draft" if _retry_keys(state["issues"], state.get("attempts", {})) else "save"

    def save(state: DraftState) -> dict:
        history = state["history"]
        caught = [i for h in history for i in h["issues"]]
        validation = {
            "passes": len(history),
            "caught": len(caught),
            "citation_caught": sum(1 for i in caught if i["check"] == "citation"),
            "redrafted": sum(1 for k, n in state["attempts"].items() if n > 1),
            "flagged": len({i["slide_key"] for i in state["issues"]}),
            "unresolved": state["issues"],
            "history": history,
        }
        draft_json = {"slides": state["slides"], "guide": state["guide"], "case": state.get("case")}
        row = store.save_draft(rt.pool, state["run_id"], draft_json, validation, rt.clock.now())
        return {"draft_version_id": row["id"], "version": row["version"]}

    def approval_gate(state: DraftState) -> dict:
        decision = interrupt(
            {
                "run_id": state["run_id"],
                "version": state["version"],
                "draft_version_id": state["draft_version_id"],
            }
        )
        if decision.get("decision") not in ("approved", "returned"):
            raise ValueError(f"unknown decision {decision!r}")
        chunk_ids = Draft.model_validate(
            {"slides": state["slides"], "guide": state["guide"], "case": state.get("case")}
        ).chunk_ids()
        approval_id = store.record_decision(
            rt.pool,
            state["draft_version_id"],
            decision["decision"],
            decision.get("reviewer", ""),
            decision.get("notes", ""),
            chunk_ids,
            rt.clock.now(),
        )
        return {"decision": {**decision, "approval_id": approval_id}}

    def after_gate(state: DraftState) -> str:
        return "export" if state["decision"]["decision"] == "approved" else END

    def export(state: DraftState) -> dict:
        from .export import export_approved

        export_approved(rt, state["draft_version_id"])
        return {}

    g = StateGraph(DraftState)
    for name, fn in [
        ("plan", plan),
        ("retrieve", retrieve),
        ("draft", draft),
        ("validate", validate),
        ("save", save),
        ("approval_gate", approval_gate),
        ("export", export),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "plan")
    g.add_edge("plan", "retrieve")
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "validate")
    g.add_conditional_edges("validate", after_validate, ["draft", "save"])
    g.add_edge("save", "approval_gate")
    g.add_conditional_edges("approval_gate", after_gate, ["export", END])
    g.add_edge("export", END)
    return g.compile(checkpointer=PostgresSaver(rt.pool))
