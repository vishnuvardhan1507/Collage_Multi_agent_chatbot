from agents.prompts import (
    RAG_ANSWER_PROMPT,
    SQL_ANSWER_PROMPT,
    SUPERVISOR_AGENT_PROMPT,
)
from db.schema import get_database_schema
from security.policy import ROLE_CAPABILITY_MATRIX
from services.llm import invoke_json, invoke_text
from workflow.state import AgentState


def supervisor_node(state: AgentState) -> AgentState:
    if state.get("guardrail_verdict") is None:
        state["agent_trace"].append("supervisor")
        return state

    if state.get("guardrail_verdict") == "deny":
        state["agent_trace"].append("supervisor")
        return state

    if state.get("route"):
        state["agent_trace"].append("supervisor")
        return state

    database_schema = get_database_schema()
    state["database_schema"] = database_schema
    result = invoke_json(
        SUPERVISOR_AGENT_PROMPT,
        {
            "user_role": state["role"],
            "user_id": state["user_id"],
            "query": state["query"],
            "chat_history": state["chat_history"],
            "database_schema": database_schema,
            "capability_matrix": ROLE_CAPABILITY_MATRIX,
        },
    )
    state["route"] = result.get("route", "direct")
    state["scoped_request"] = result.get("scoped_request")
    state["direct_answer"] = result.get("direct_answer")
    state["agent_trace"].append("supervisor")
    return state


def supervisor_aggregate_node(state: AgentState) -> AgentState:
    if state.get("final_response"):
        state["agent_trace"].append("supervisor")
        return state

    route = state.get("route")
    if route == "direct":
        state["final_response"] = state.get("direct_answer") or "I can help with college attendance, courses, results, leave, classrooms, and handbook questions."
    elif route == "rag":
        state["final_response"] = invoke_text(
            RAG_ANSWER_PROMPT,
            {
                "query": state["query"],
                "context": state.get("rag_context") or "",
                "chat_history": state["chat_history"],
            },
            temperature=0.2,
        )
    elif route == "sql":
        if state.get("validation_verdict") == "invalid":
            state["final_response"] = "I couldn't safely retrieve that. Please rephrase or contact IT support."
        elif not state.get("sql_query"):
            state["final_response"] = state.get("validation_feedback") or "That action is not permitted."
        else:
            state["final_response"] = invoke_text(
                SQL_ANSWER_PROMPT,
                {
                    "query": state["query"],
                    "scoped_request": state.get("scoped_request"),
                    "sql_result": state.get("sql_result"),
                    "role": state["role"],
                },
                temperature=0.2,
            )
    else:
        state["final_response"] = "I could not determine how to route that request."
    state["agent_trace"].append("supervisor")
    return state
