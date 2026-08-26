from pathlib import Path
from typing import Optional

from langgraph.graph import END, StateGraph

from agents.guardrail_agent import guardrail_node
from agents.sql_query_agent import sql_execute_node, sql_query_node
from agents.supervisor_agent import supervisor_aggregate_node, supervisor_node
from agents.validator_agent import validator_node
from tools.memory_tool import append_turn
from tools.rag_tool import query as rag_query
from workflow.state import AgentState

def _after_supervisor(state: AgentState) -> str:
    if state.get("guardrail_verdict") is None:
        return "guardrail"
    if state.get("guardrail_verdict") == "deny":
        return "aggregate"

    route = state.get("route")
    if route == "sql":
        if state.get("sql_result") is not None:
            return "aggregate"
        if state.get("validation_verdict") == "valid":
            return "execute_sql"
        if state.get("validation_verdict") == "invalid":
            if state.get("retry_count", 0) < 3:
                return "sql_query_agent"
            return "aggregate"
        if state.get("sql_query") or state.get("validation_feedback"):
            return "validator"
        return "sql_query_agent"
    if route == "rag":
        return "aggregate" if state.get("rag_context") else "rag"
    if route == "direct":
        return "aggregate"
    return "aggregate"

def rag_node(state: AgentState) -> AgentState:
    state["rag_context"] = rag_query(state["query"], k=4)
    state["agent_trace"].append("rag_tool")
    return state


def memory_write_node(state: AgentState) -> AgentState:
    append_turn(state["user_id"], state["session_id"], "user", state["query"])
    append_turn(state["user_id"], state["session_id"], "assistant", state.get("final_response") or "")
    state["agent_trace"].append("memory_write")
    return state


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("guardrail", guardrail_node)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("rag", rag_node)
    graph.add_node("sql_query_agent", sql_query_node)
    graph.add_node("validator", validator_node)
    graph.add_node("execute_sql", sql_execute_node)
    graph.add_node("aggregate", supervisor_aggregate_node)
    graph.add_node("memory_write", memory_write_node)

    graph.set_entry_point("supervisor")
    graph.add_edge("guardrail", "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _after_supervisor,
        {
            "guardrail": "guardrail",
            "rag": "rag",
            "sql_query_agent": "sql_query_agent",
            "validator": "validator",
            "execute_sql": "execute_sql",
            "aggregate": "aggregate",
        },
    )
    graph.add_edge("rag", "aggregate")
    graph.add_edge("sql_query_agent", "supervisor")
    graph.add_edge("validator", "supervisor")
    graph.add_edge("execute_sql", "supervisor")
    graph.add_edge("aggregate", "memory_write")
    graph.add_edge("memory_write", END)
    return graph.compile()


def visualize_graph(output_path: Optional[str] = None) -> str:
    """Return a Mermaid diagram for the LangGraph workflow, optionally saving it."""
    mermaid = build_graph().get_graph().draw_mermaid()
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(mermaid, encoding="utf-8")
    return mermaid
