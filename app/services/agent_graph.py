"""Bounded LangGraph workflow for proposing, critiquing, and saving questions."""

import json
from typing import Any, TypedDict
from uuid import UUID

from pydantic import BaseModel

from app.schemas.examples import SimilarExampleQuestions
from app.schemas.extraction import FinalAgentQuestion, QuestionCritique, QuestionGeneration
from app.schemas.papers import (
    AgenticQuestionWorkflowResponse,
    PaperDetail,
    PaperReviewContextResponse,
    QuestionGenerationResponse,
)
from app.services.agent_policy import (
    DEFAULT_QUESTIONS_PER_RUN,
    MAX_REVISIONS_PER_QUESTION,
    MAX_TOOL_CALLS_PER_RUN,
    build_agent_system_prompt,
)
from app.services.agent_tools import ExtractPaperFieldsOutput, create_paper_tools


class AgentState(TypedDict, total=False):
    paper_id: UUID
    target_count: int
    system_prompt: str
    tool_calls: int
    paper: PaperDetail
    title: str
    abstract: str
    paper_text: str
    extracted_fields: dict[str, dict]
    reviews: list[dict]
    example_questions: list[dict]
    candidates: list[dict]
    current_candidate: dict
    current_critique: QuestionCritique
    revisions: int
    next_candidate_number: int
    accepted: list[dict]
    proposal_round: int
    saved_response: QuestionGenerationResponse


def _as_model(value: Any, model: type[BaseModel]) -> Any:
    if isinstance(value, model):
        return value
    if isinstance(value, str):
        return model.model_validate_json(value)
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        try:
            return model.model_validate_json(value["content"])
        except ValueError:
            pass
    return model.model_validate(value)


def _merge_stored_fields(paper: PaperDetail) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for field in paper.extracted_fields:
        value = field.value or {}
        if "text" in value:
            merged.setdefault(field.field_type, {"text": ""})
            if not merged[field.field_type]["text"]:
                merged[field.field_type]["text"] = value.get("text", "")
        elif "items" in value:
            target = merged.setdefault(field.field_type, {"items": []})["items"]
            for item in value.get("items") or []:
                if item not in target:
                    target.append(item)
    return merged


def _context_abstract(paper: PaperDetail) -> str:
    abstract = next(
        (section.content for section in paper.sections
         if (section.heading or "").strip().lower() == "abstract"),
        "",
    )
    return abstract.strip()


async def run_agentic_question_workflow(
    paper_id: UUID,
    session_factory,
    *,
    report=None,
    question_count: int = DEFAULT_QUESTIONS_PER_RUN,
) -> AgenticQuestionWorkflowResponse:
    """Run the fixed context → proposal → critique → conditional revise/save graph.

    The generation and critique tools call Gemini for their structured outputs.
    The graph uses the critique model's keep/revise/reject verdict to route each
    candidate and enforces revision, total-tool-call, and terminal-save limits.
    """
    from langgraph.graph import END, StateGraph

    if not 3 <= question_count <= 10:
        raise ValueError("question_count must be between 3 and 10.")

    tools = {item.name: item for item in create_paper_tools(session_factory)}

    async def emit(step_id: str, label: str, status: str, detail: str) -> None:
        if report:
            await report(step_id, label, status, detail)

    async def invoke_tool(name: str, payload: dict, calls: int) -> tuple[Any, int]:
        is_terminal_save = name == "save_final_questions"
        limit = MAX_TOOL_CALLS_PER_RUN if is_terminal_save else MAX_TOOL_CALLS_PER_RUN - 1
        if calls >= limit:
            raise RuntimeError("The agent reached its tool-call limit before it could continue.")
        result = await tools[name].ainvoke(payload)
        return result, calls + 1

    async def gather_context(state: AgentState) -> dict:
        calls = state.get("tool_calls", 0)
        await emit("agent-context", "Gather paper context", "running", "Loading the prepared paper.")
        raw_paper, calls = await invoke_tool(
            "get_stored_paper", {"paper_id": state["paper_id"]}, calls
        )
        paper = _as_model(raw_paper, PaperDetail)
        title = paper.title or "Untitled paper"
        abstract = _context_abstract(paper)
        paper_text = paper.full_text or ""
        fields = _merge_stored_fields(paper)
        if not abstract and not paper_text.strip() and not fields:
            raise ValueError("This paper has no abstract, full text, or extracted fields for the agentic workflow.")
        latest_version_key = next(
            (version.version_key for version in paper.versions if version.id == paper.latest_version_id),
            None,
        )
        version_detail = f" ({latest_version_key})" if latest_version_key else ""
        await emit(
            "agent-context",
            "Gather paper context",
            "success",
            f"Loaded {title}{version_detail}"
            f" with {len(fields)} stored extracted field types.",
        )

        await emit("agent-reviews", "Gather review context", "running", "Reading reviews across all revisions.")
        raw_reviews, calls = await invoke_tool(
            "get_all_reviews_for_paper", {"paper_id": state["paper_id"]}, calls
        )
        review_response = _as_model(raw_reviews, PaperReviewContextResponse)
        reviews = [item.model_dump(mode="json") for item in review_response.reviews]
        await emit(
            "agent-reviews",
            "Gather review context",
            "success",
            f"Loaded {len(reviews)} reviews across stored revisions.",
        )

        if not fields and (paper_text.strip() or abstract):
            await emit("agent-extract", "Check extracted fields", "running", "No reusable structured fields were found; extracting them.")
            calls_before_extract = calls
            try:
                raw_extraction, calls = await invoke_tool(
                    "extract_paper_fields",
                    {"title": title, "full_text": paper_text, "abstract": abstract},
                    calls,
                )
                fields = _as_model(raw_extraction, ExtractPaperFieldsOutput).extracted_fields
                await emit("agent-extract", "Check extracted fields", "success", f"Prepared {len(fields)} field types.")
            except Exception as exc:  # noqa: BLE001 - extraction failure can fall back to paper text.
                # Extraction is useful context, but the paper text/abstract remain valid input.
                if calls == calls_before_extract:
                    calls += 1
                await emit("agent-extract", "Check extracted fields", "warning", f"Extraction was unavailable: {exc}")
        else:
            await emit("agent-extract", "Check extracted fields", "success", "Reusing the paper's stored extracted fields.")

        example_context = "\n\n".join([
            title,
            abstract,
            json.dumps(fields, ensure_ascii=False),
        ]).strip()
        examples: list[dict] = []
        await emit("agent-examples", "Find similar-paper examples", "running", "Searching paper-conditioned examples.")
        calls_before_examples = calls
        try:
            raw_examples, calls = await invoke_tool(
                "retrieve_similar_example_questions",
                {"paper_context": example_context, "k": 5},
                calls,
            )
            examples_response = _as_model(raw_examples, SimilarExampleQuestions)
            examples = [item.model_dump(mode="json") for item in examples_response.examples]
            await emit("agent-examples", "Find similar-paper examples", "success", f"Retrieved {len(examples)} examples.")
        except Exception as exc:  # noqa: BLE001 - example retrieval is optional for this run.
            if calls == calls_before_examples:
                calls += 1
            await emit("agent-examples", "Find similar-paper examples", "warning", f"Continuing without examples: {exc}")

        return {
            "tool_calls": calls,
            "paper": paper,
            "title": title,
            "abstract": abstract,
            "paper_text": paper_text,
            "extracted_fields": fields,
            "reviews": reviews,
            "example_questions": examples,
            "system_prompt": build_agent_system_prompt(state["target_count"]),
            "candidates": [],
            "accepted": [],
            "next_candidate_number": 1,
            "proposal_round": 0,
        }

    async def generate(state: AgentState) -> dict:
        needed = state["target_count"] - len(state.get("accepted", []))
        if needed <= 0:
            return {"candidates": []}
        proposal_round = state.get("proposal_round", 0) + 1
        step_id = f"agent-propose-{proposal_round}"
        await emit(step_id, "Propose questions", "running", f"Proposing up to {needed} questions.")
        raw, calls = await invoke_tool(
            "propose_questions",
            {
                "title": state["title"],
                "abstract": state["abstract"],
                "extracted_fields": state.get("extracted_fields", {}),
                "count": needed,
                "paper_text": state.get("paper_text", ""),
                "review_context": state.get("reviews", []),
                "example_questions": state.get("example_questions", []),
                "workflow_instructions": state["system_prompt"],
            },
            state.get("tool_calls", 0),
        )
        generation = _as_model(raw, QuestionGeneration)
        proposals = generation.questions
        start_number = state.get("next_candidate_number", 1)
        candidates = [
            {
                "question": item.question,
                "focus": item.focus,
                "rationale": item.rationale,
                "candidate_number": start_number + offset,
            }
            for offset, item in enumerate(proposals)
        ]
        await emit(step_id, "Propose questions", "success", f"Proposed {len(candidates)} candidates.")
        return {
            "tool_calls": calls,
            "proposal_round": proposal_round,
            "next_candidate_number": start_number + len(candidates),
            "candidates": candidates,
            "revisions": 0,
        }

    async def critique(state: AgentState) -> dict:
        candidate = state["candidates"][0]
        round_number = state.get("revisions", 0) + 1
        candidate_number = candidate["candidate_number"]
        step_id = f"agent-critique-{candidate_number}-{round_number}"
        question_preview = candidate["question"][:240]
        await emit(
            step_id,
            f"Critique question {candidate_number}",
            "running",
            f"Assessing round {round_number} of at most {1 + MAX_REVISIONS_PER_QUESTION}: {question_preview}",
        )
        raw, calls = await invoke_tool(
            "critique_question",
            {
                "question": candidate["question"],
                "title": state["title"],
                "abstract": state["abstract"],
                "extracted_fields": state.get("extracted_fields", {}),
                "paper_text": state.get("paper_text", ""),
                "review_context": state.get("reviews", []),
                "workflow_instructions": state["system_prompt"],
            },
            state.get("tool_calls", 0),
        )
        critique_result = _as_model(raw, QuestionCritique)
        scores = (
            f"specificity {critique_result.specificity_score}/5, "
            f"grounding {critique_result.grounding_score}/5, "
            f"answerability {critique_result.answerability_score}/5, "
            f"critical value {critique_result.critical_value_score}/5"
        )
        summary = critique_result.rationale
        if critique_result.issues:
            summary += " Issues: " + "; ".join(critique_result.issues)
        await emit(
            step_id,
            f"Critique question {candidate_number}",
            "success",
            f"Model verdict: {critique_result.verdict}. Scores: {scores}. {summary}",
        )
        return {"tool_calls": calls, "current_critique": critique_result}

    async def revise(state: AgentState) -> dict:
        candidate = dict(state["candidates"][0])
        critique_result = state["current_critique"]
        candidate_number = candidate["candidate_number"]
        step_id = f"agent-revise-{candidate_number}-{state.get('revisions', 0) + 1}"
        await emit(step_id, f"Revise question {candidate_number}", "running", "Using the model's suggested grounded revision.")
        candidate["question"] = critique_result.revised_question.strip()
        candidate["rationale"] = critique_result.rationale
        await emit(step_id, f"Revise question {candidate_number}", "success", "Revised candidate; sending it back for critique.")
        return {"candidates": [candidate, *state["candidates"][1:]], "revisions": state.get("revisions", 0) + 1}

    async def advance(state: AgentState) -> dict:
        accepted = list(state.get("accepted", []))
        critique_result = state["current_critique"]
        candidate = state["candidates"][0]
        if critique_result.verdict == "keep":
            accepted.append({
                "question": candidate["question"],
                "focus": candidate["focus"],
                "rationale": candidate["rationale"],
                "critique": critique_result.model_dump(mode="json"),
            })
        remaining = state["candidates"][1:]
        patch: dict[str, Any] = {
            "accepted": accepted,
            "candidates": remaining,
            "revisions": 0,
        }
        if remaining:
            patch["current_candidate"] = remaining[0]
        await emit(
            f"agent-candidate-{candidate['candidate_number']}-done",
            f"Finish question {candidate['candidate_number']}",
            "success",
            "Accepted by the model's keep verdict." if critique_result.verdict == "keep" else "Candidate dropped after the model's critique.",
        )
        return patch

    async def save(state: AgentState) -> dict:
        accepted = state.get("accepted", [])
        if not accepted:
            await emit("agent-finalize", "Finalize workflow", "success", "No questions met the critique quality bar; nothing was saved.")
            return {}
        await emit("agent-save", "Save final questions", "running", f"Saving {len(accepted)} model-accepted questions.")
        questions = [FinalAgentQuestion.model_validate(item) for item in accepted]
        raw, calls = await invoke_tool(
            "save_final_questions",
            {"paper_id": state["paper_id"], "questions": questions},
            state.get("tool_calls", 0),
        )
        saved = _as_model(raw, QuestionGenerationResponse)
        await emit("agent-save", "Save final questions", "success", f"Saved {len(saved.questions)} questions as final.")
        return {"tool_calls": calls, "saved_response": saved}

    def after_critique(state: AgentState) -> str:
        result = state["current_critique"]
        if result.verdict == "revise" and result.revised_question and state.get("revisions", 0) < MAX_REVISIONS_PER_QUESTION:
            return "revise"
        return "advance"

    def after_advance(state: AgentState) -> str:
        if len(state.get("accepted", [])) >= state["target_count"]:
            return "save"
        if state.get("tool_calls", 0) >= MAX_TOOL_CALLS_PER_RUN - 1:
            return "save"
        if state.get("candidates"):
            return "critique"
        return "generate"

    graph = StateGraph(AgentState)
    graph.add_node("context", gather_context)
    graph.add_node("generate", generate)
    graph.add_node("critique", critique)
    graph.add_node("revise", revise)
    graph.add_node("advance", advance)
    graph.add_node("save", save)
    graph.set_entry_point("context")
    graph.add_edge("context", "generate")
    graph.add_edge("generate", "critique")
    graph.add_conditional_edges("critique", after_critique, {"revise": "revise", "advance": "advance"})
    graph.add_edge("revise", "critique")
    graph.add_conditional_edges(
        "advance",
        after_advance,
            {"save": "save", "critique": "critique", "generate": "generate"},
    )
    graph.add_edge("save", END)
    compiled = graph.compile()
    final_state = await compiled.ainvoke({
        "paper_id": paper_id,
        "target_count": question_count,
        "system_prompt": build_agent_system_prompt(question_count),
        "tool_calls": 0,
        "revisions": 0,
        "accepted": [],
        "candidates": [],
    })
    saved_response = final_state.get("saved_response")
    questions = saved_response.questions if saved_response else []
    count_saved = len(questions)
    if count_saved == question_count:
        outcome = "target_met"
    elif count_saved:
        outcome = "saved_partial_result"
    else:
        outcome = "no_questions_passed"
    return AgenticQuestionWorkflowResponse(
        paper_id=paper_id,
        requested_count=question_count,
        saved_count=count_saved,
        tool_calls=final_state.get("tool_calls", 0),
        outcome=outcome,
        questions=questions,
    )
