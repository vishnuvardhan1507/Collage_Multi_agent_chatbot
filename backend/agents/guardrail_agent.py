from agents import llm
from agents.prompts import GUARDRAIL_AGENT_PROMPT, ROLE_CAPABILITY_MATRIX, validate_modules
from tools.db_tool import fetch_one

def guardrail_node(state):
    user = fetch_one("SELECT role FROM users WHERE user_id = ?", (state["user_id"],))
    if not user or user["role"] != state["role"]:
        result = {"verdict": "deny", "reason": "Account or role is no longer valid."}
    else:
        result = llm.invoke_json(GUARDRAIL_AGENT_PROMPT, {
            "user_role": state["role"], "user_id": state["user_id"], "query": state["query"],
            "chat_history": state["chat_history"], "capability_matrix": ROLE_CAPABILITY_MATRIX,
            "last_timetable_context": state.get('last_timetable_context'),
        })
    verdict = "allow" if result.get("verdict") == "allow" else "deny"
    scope = result.get('topic_scope')
    if scope == 'unrelated':
        verdict = 'deny'
        reason = result.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('Guardrail did not generate a response for the unrelated request.')
        result['reason'] = reason.strip()
    elif verdict == 'allow' and (not isinstance(scope,str) or scope not in {'college', 'social'}):
        verdict = 'deny'
        result['reason'] = 'I could not identify a supported college request. Please clarify your college-related question.'
    state['modules'] = validate_modules(result.get('modules'))
    state.update(guardrail_verdict=verdict, guardrail_reason=result.get("reason") or "Unable to authorize this request.")
    if verdict == "deny":
        state["final_response"] = state["guardrail_reason"]
    state["agent_trace"].append("guardrail")
    return state
