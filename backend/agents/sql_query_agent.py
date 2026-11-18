from agents.prompts import scoped_prompt, scoped_payload
from agents import llm
from agents.prompts import DATABASE_SCHEMA, ROLE_CAPABILITY_MATRIX, SQL_QUERY_AGENT_PROMPT


def sql_query_node(state):
    state["retry_count"] = state.get("retry_count", 0) + 1
    state.update(sql_query=None, operation=None, projection_date=None, projection_request=None, timetable_request=None, compiled_sql=None, parameters=None, operation_evidence=None)
    try:
        result = llm.invoke_json(scoped_prompt(SQL_QUERY_AGENT_PROMPT, state), scoped_payload({
            "scoped_request": state["scoped_request"], "query": state["query"],
            "user_id": state["user_id"], "role": state["role"], "database_schema": DATABASE_SCHEMA,
            "capability_matrix": ROLE_CAPABILITY_MATRIX, "chat_history": state["chat_history"],
            "current_date": state["current_date"], "timezone": state["timezone"],
            "current_datetime": state.get('current_datetime'),
            "validation_feedback": state.get("validation_feedback"),
            "transport_catalog": state.get('transport_catalog'),
            "timetable_catalog": state.get('timetable_catalog'),
        }, state))
        if result.get("clarification"):
            state.update(terminal=True, final_response=str(result["clarification"]))
        else:
            state.update(sql_query=result.get("sql"), operation=result.get("operation"), projection_date=result.get("projection_date"), projection_request=result.get('projection_request'), timetable_request=result.get('timetable_request'))
        state["validation_feedback"] = None
    except (ValueError, TypeError):
        state["validation_feedback"] = "Return one valid JSON candidate with SQL, an operation, or a projection date."
    state["agent_trace"].append("sql_query_agent")
    return state
