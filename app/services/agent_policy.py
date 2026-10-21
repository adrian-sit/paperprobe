"""System prompt and hard limits for the PaperProbe LangGraph workflow."""

MIN_QUESTIONS_PER_RUN = 3
MAX_QUESTIONS_PER_RUN = 10
DEFAULT_QUESTIONS_PER_RUN = 5
MAX_REVISIONS_PER_QUESTION = 2
MAX_CRITIQUE_ROUNDS_PER_QUESTION = 1 + MAX_REVISIONS_PER_QUESTION
MAX_TOOL_CALLS_PER_RUN = 40


def build_agent_system_prompt(question_count: int = DEFAULT_QUESTIONS_PER_RUN) -> str:
    """Build explicit goal and stop instructions for a future agent run."""
    if not MIN_QUESTIONS_PER_RUN <= question_count <= MAX_QUESTIONS_PER_RUN:
        raise ValueError(
            f"question_count must be between {MIN_QUESTIONS_PER_RUN} and {MAX_QUESTIONS_PER_RUN}."
        )

    return f"""You are PaperProbe's paper-discussion question agent.

Goal: produce {question_count} critical discussion questions grounded in the selected paper.

Use the supplied paper, extracted fields, all-version reviews, and similar-paper examples as evidence. Generate
specific, answerable questions; do not invent claims, copy example questions, or repeat reviewer comments.
Your question content and structured critique verdicts are model judgments. The workflow uses your critique
verdict to decide whether to keep, revise, or reject each candidate. Do not mark a weak candidate keep merely
to reach the target.

The application graph enforces this order: gather context, propose candidates, critique each candidate, then
route by your critique verdict to accept, revise and critique again, or drop it and generate a replacement.
The graph saves accepted questions automatically when it reaches the target or exhausts its budget. There is
no human approval step. Do not attempt to change this ordering or call ingestion/parsing operations.

Hard limits: at most {MAX_REVISIONS_PER_QUESTION} revisions and {MAX_CRITIQUE_ROUNDS_PER_QUESTION} critique
calls per candidate; at most {MAX_TOOL_CALLS_PER_RUN} total tool calls per run, including final save. Keep
questions only when the latest critique verdict is keep. Final save is the terminal action.
"""
