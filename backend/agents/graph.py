import sqlite3

from langgraph.graph import END, StateGraph

from agents.guardrail_agent import guardrail_node
from agents.sql_query_agent import sql_query_node
from agents.state import AgentState
from agents.supervisor_agent import supervisor_aggregate_node, supervisor_intake_node, supervisor_node
from agents.validator_agent import validator_node
from tools.db_tool import execute_query, read_menu
from tools.operations import OperationError, eligible_courses, execute_operation, project_attendance
from tools.confirmations import propose
from tools.rag_tool import retrieve
from tools.timetable import read_timetable, read_teaching_load
from tools.academic_guidance import read_guidance
from datetime import datetime


def _after_validator(state):
    if state.get("terminal"):
        return "aggregate"
    if state.get("validation_verdict") == "valid":
        return "execute_sql"
    return "sql_query_agent" if state["retry_count"] < 3 else "aggregate"


def rag_node(state):
    try:
        result = retrieve(state["retrieval_query"], k=4)
        state.update(rag_context=result["context"], sources=result["sources"])
    except Exception:
        state.update(rag_context="Knowledge-base retrieval is temporarily unavailable.", sources=[])
    state["agent_trace"].append("rag_tool")
    return state


def menu_node(state):
    try:
        state['menu_result'] = read_menu(state['menu_request'], state['user_id'], state['role'])
    except PermissionError:
        state['menu_error'] = 'Unable to authorize this menu request.'
    except Exception:
        state['menu_error'] = 'The menu information is temporarily unavailable. Please try again.'
    state['agent_trace'].append('menu_tool')
    return state


def course_eligibility_node(state):
    try:
        state['course_eligibility_result'] = eligible_courses(state['user_id'], state['role'])
    except PermissionError as exc:
        state['final_response'] = str(exc)
    except Exception:
        state['final_response'] = 'Course eligibility information is temporarily unavailable. Please try again.'
    state['agent_trace'].append('course_eligibility_tool')
    return state


def teaching_load_node(state):
    try:
        now = datetime.fromisoformat(state['current_datetime']) if state.get('current_datetime') else None
        state['teaching_load_result'] = read_teaching_load(state['teaching_load_request'], state['user_id'], state['role'], now=now)
    except (PermissionError, OperationError) as exc:
        state['final_response'] = str(exc)
    except Exception:
        state['final_response'] = 'Teaching-load information is temporarily unavailable. Please try again.'
    state['agent_trace'].append('teaching_load_tool')
    return state


def planned_next(state):
    if state.get('academic_guidance_request') is not None:
        return 'academic_guidance'
    if state.get('leave_draft_ready'):
        return 'execute_sql'
    if state.get('timetable_request') is not None and state['route'] == 'sql':
        return 'execute_sql'
    return {'sql':'sql_query_agent', 'rag':'rag', 'both':'rag', 'direct':'aggregate', 'menu':'aggregate'}[state['route']]


def academic_guidance_node(state):
    try:
        state['academic_guidance_result'] = read_guidance(state['academic_guidance_request'], state['user_id'], state['role'])
    except (PermissionError, ValueError) as exc:
        state['final_response'] = str(exc)
    except Exception:
        state['final_response'] = 'Academic guidance information is temporarily unavailable. Please try again.'
    state['agent_trace'].append('academic_guidance_tool')
    return state


def execute_sql_node(state):
    try:
        if state.get("operation") is not None:
            if state['operation'].get('type') in {'submit_leave','book_classroom'}:
                if state.get('leave_draft_ready'):
                    state['confirmation'] = propose(state['operation'],state['user_id'],state['role'],state['session_id'], expected_leave_revision=state['leave_draft_revision'])
                else:
                    state['confirmation'] = propose(state['operation'],state['user_id'],state['role'],state['session_id'])
                state.update(terminal=True, final_response=state['confirmation']['summary'] + '\nPlease confirm or cancel below.')
                state['agent_trace'].append('confirmation_proposed')
                return state
            state["sql_result"] = execute_operation(state["operation"], state["user_id"], state["role"])
            state["actions"] = [state["sql_result"]["action"]]
            state["terminal"] = True  # A committed write never re-enters the retry loop.
        elif state.get('timetable_request') is not None:
            now = datetime.fromisoformat(state['current_datetime']) if state.get('current_datetime') else None
            state['sql_result'] = read_timetable(state['timetable_request'], state['user_id'], state['role'], now=now)
        elif state.get('projection_request') is not None:
            state['sql_result'] = project_attendance(state['user_id'], state['role'], state['projection_request'])
        elif state.get("projection_date"):
            state["sql_result"] = project_attendance(state["user_id"], state["role"], state["projection_date"])
        else:
            state["sql_result"] = execute_query(state["sql_query"], state["user_id"], state["role"], allow_menus=False)
    except (PermissionError, OperationError) as exc:
        state.update(terminal=True, validation_verdict="invalid", final_response=str(exc), validation_feedback=str(exc))
    except (sqlite3.Error, ValueError) as exc:
        state.update(validation_verdict="invalid", validation_feedback=str(exc), sql_result=None)
        if state.get("operation") is not None:
            state.update(terminal=True, final_response="The operation could not be saved. Please check its details.")
    state["agent_trace"].append("db_tool")
    return state


def _after_execute(state):
    if not state.get("terminal") and state.get("validation_verdict") == "invalid" and state["retry_count"] < 3:
        return "sql_query_agent"
    return "aggregate"


def build_graph():
    graph = StateGraph(AgentState)
    for name, node in {
        "supervisor_intake": supervisor_intake_node, "guardrail": guardrail_node,
        "supervisor": supervisor_node, "rag": rag_node, "sql_query_agent": sql_query_node,
        "validator": validator_node, "execute_sql": execute_sql_node, "aggregate": supervisor_aggregate_node,
        "menu": menu_node,
        "course_eligibility": course_eligibility_node,
        "teaching_load": teaching_load_node,
        "academic_guidance": academic_guidance_node,
    }.items():
        graph.add_node(name, node)
    graph.set_entry_point("supervisor_intake")
    graph.add_edge("supervisor_intake", "guardrail")
    graph.add_conditional_edges("guardrail", lambda s: END if s["guardrail_verdict"] == "deny" else "supervisor", {END: END, "supervisor": "supervisor"})
    graph.add_conditional_edges("supervisor", lambda s: 'teaching_load' if s.get('teaching_load_request') else ('course_eligibility' if s.get('course_eligibility_request') else ('menu' if s.get('menu_request') else planned_next(s))), {x:x for x in ('academic_guidance','teaching_load','course_eligibility','menu','sql_query_agent','rag','aggregate','execute_sql')})
    graph.add_edge('academic_guidance', 'aggregate')
    graph.add_edge('course_eligibility', 'aggregate')
    graph.add_edge('teaching_load', 'aggregate')
    graph.add_conditional_edges('menu', planned_next, {x:x for x in ('sql_query_agent','rag','aggregate','execute_sql')})
    graph.add_conditional_edges("rag", lambda s: ('execute_sql' if s.get('timetable_request') is not None else 'sql_query_agent') if s['route']=='both' else 'aggregate', {x:x for x in ('sql_query_agent','execute_sql','aggregate')})
    graph.add_edge("sql_query_agent", "validator")
    graph.add_conditional_edges("validator", _after_validator, {x: x for x in ("execute_sql", "sql_query_agent", "aggregate")})
    graph.add_conditional_edges("execute_sql", _after_execute, {"sql_query_agent": "sql_query_agent", "aggregate": "aggregate"})
    graph.add_edge("aggregate", END)
    return graph.compile()
