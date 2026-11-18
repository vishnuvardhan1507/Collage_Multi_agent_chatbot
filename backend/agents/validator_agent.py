from agents.prompts import scoped_prompt, scoped_payload
from agents import llm
from agents.prompts import DATABASE_SCHEMA, ROLE_CAPABILITY_MATRIX, VALIDATOR_AGENT_PROMPT
from tools.operations import OperationError, compile_operation, parse_date, resolve_leave_deletion, validate_projection_request
from tools.timetable import validate_timetable_request


def validator_node(state):
    state["agent_trace"].append("validator_agent")
    state["validation_verdict"] = "invalid"
    if state.get("terminal"):
        return state
    candidates = [state.get("sql_query"), state.get("operation"), state.get("projection_date"), state.get('projection_request'), state.get('timetable_request')]
    if sum(value is not None for value in candidates) != 1:
        state["validation_feedback"] = state.get("validation_feedback") or "Return exactly one nonempty candidate."
        return state
    try:
        if state.get("operation") is not None:
            if isinstance(state['operation'], dict) and state['operation'].get('type') == 'delete_leave':
                operation, evidence = resolve_leave_deletion(state['operation'], state['user_id'], state['role'])
                state.update(operation=operation, operation_evidence=evidence)
            sql, params = compile_operation(state["operation"], state["user_id"], state["role"])
            state.update(compiled_sql=sql, parameters=params)
        elif state.get('timetable_request') is not None:
            validate_timetable_request(state['timetable_request'])
        elif state.get('projection_request') is not None:
            validate_projection_request(state['projection_request'])
        elif state.get("projection_date") is not None:
            parse_date(state["projection_date"])
        elif not isinstance(state["sql_query"], str) or not state["sql_query"].strip():
            raise ValueError("Generate a nonempty SELECT query.")
        elif state["sql_query"].lstrip().split(None, 1)[0].lower() not in {"select", "with"}:
            raise PermissionError("Arbitrary generated writes and administrative SQL are forbidden.")
        result = llm.invoke_json(scoped_prompt(VALIDATOR_AGENT_PROMPT, state), scoped_payload({
            "sql": state.get("sql_query"), "operation": state.get("operation"),
            "projection_date": state.get("projection_date"), "compiled_sql": state.get("compiled_sql"),
            "projection_request": state.get('projection_request'),
            "timetable_request": state.get('timetable_request'),
            "timetable_catalog": state.get('timetable_catalog'),
            "parameters": state.get("parameters"), "query": state["query"],
            "scoped_request": state["scoped_request"], "user_id": state["user_id"], "role": state["role"],
            "chat_history": state["chat_history"], "database_schema": DATABASE_SCHEMA,
            "capability_matrix": ROLE_CAPABILITY_MATRIX,
            "operation_evidence": state.get("operation_evidence"),
            "transport_catalog": state.get('transport_catalog'),
            "current_date": state["current_date"], "timezone": state["timezone"],
        }, state))
        state["validation_verdict"] = "valid" if result.get("verdict") == "valid" else "invalid"
        state["validation_feedback"] = result.get("feedback") or "Check syntax and whether the candidate answers the request."
    except (PermissionError, OperationError) as exc:
        state.update(terminal=True, final_response=str(exc), validation_feedback=str(exc))
    except (ValueError, TypeError) as exc:
        state["validation_feedback"] = str(exc)
    return state
