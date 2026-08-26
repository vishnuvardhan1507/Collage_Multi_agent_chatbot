from agents.prompts import VALIDATOR_AGENT_PROMPT
from security.policy import ROLE_CAPABILITY_MATRIX
from services.llm import invoke_json
from workflow.state import AgentState


def validator_node(state: AgentState) -> AgentState:
    if not state.get("sql_query"):
        state["validation_verdict"] = "invalid"
        state["validation_feedback"] = state.get("validation_feedback") or "No SQL was generated."
        state["agent_trace"].append("validator_agent")
        return state

    result = invoke_json(
        VALIDATOR_AGENT_PROMPT,
        {
            "sql": state["sql_query"],
            "scoped_request": state.get("scoped_request"),
            "user_id": state["user_id"],
            "role": state["role"],
            "database_schema": state.get("database_schema") or "",
            "capability_matrix": ROLE_CAPABILITY_MATRIX,
        },
    )
    state["validation_verdict"] = result.get("verdict", "invalid")
    state["validation_feedback"] = result.get("feedback")
    state["agent_trace"].append("validator_agent")
    if state["validation_verdict"] == "invalid":
        state["retry_count"] = state.get("retry_count", 0) + 1
    return state
