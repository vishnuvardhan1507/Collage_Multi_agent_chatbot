from agents.prompts import GUARDRAIL_AGENT_PROMPT
from security.policy import ROLE_CAPABILITY_MATRIX
from services.llm import invoke_json
from workflow.state import AgentState


def guardrail_node(state: AgentState) -> AgentState:
    result = invoke_json(
        GUARDRAIL_AGENT_PROMPT,
        {
            "user_role": state["role"],
            "user_id": state["user_id"],
            "query": state["query"],
            "capability_matrix": ROLE_CAPABILITY_MATRIX,
        },
    )
    state["guardrail_verdict"] = result.get("verdict", "deny")
    state["guardrail_reason"] = result.get("reason", "Unable to validate request.")
    state["agent_trace"].append("guardrail")
    if state["guardrail_verdict"] == "deny":
        state["final_response"] = state["guardrail_reason"]
    return state
