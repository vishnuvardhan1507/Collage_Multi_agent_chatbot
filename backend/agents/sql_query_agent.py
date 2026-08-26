import sqlite3

from agents.prompts import SQL_QUERY_AGENT_PROMPT
from services.llm import invoke_json
from tools.db_tool import execute_query
from workflow.state import AgentState


def sql_query_node(state: AgentState) -> AgentState:
    result = invoke_json(
        SQL_QUERY_AGENT_PROMPT,
        {
            "scoped_request": state.get("scoped_request"),
            "user_id": state["user_id"],
            "role": state["role"],
            "database_schema": state.get("database_schema") or "",
            "validation_feedback": state.get("validation_feedback"),
        },
    )
    state["sql_query"] = result.get("sql")
    state["validation_verdict"] = None
    state["validation_feedback"] = result.get("explanation")
    state["agent_trace"].append("sql_query_agent")
    return state


def sql_execute_node(state: AgentState) -> AgentState:
    try:
        state["sql_result"] = execute_query(state["sql_query"], state["user_id"], state["role"])
    except (PermissionError, sqlite3.Error) as exc:
        state["validation_verdict"] = "invalid"
        state["validation_feedback"] = str(exc)
        state["sql_result"] = None
    state["agent_trace"].append("sql_query_agent_execute")
    return state
