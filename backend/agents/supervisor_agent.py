from agents.prompts import scoped_prompt, scoped_payload
import re
from datetime import date, datetime, timedelta

from agents import llm
from agents.prompts import ANSWER_PROMPT, DATABASE_SCHEMA, ROLE_CAPABILITY_MATRIX, SUPERVISOR_AGENT_PROMPT, MENU_ANSWER_PROMPT
from tools.db_tool import menu_catalog, validate_menu_request, UnsupportedMenuChoice, transport_catalog
from agents.response_formatter import format_sql_response, format_menu_response
from tools.timetable import college_now, format_timetable, format_teaching_load, validate_timetable_request, validate_teaching_load_request, timetable_catalog, valid_timetable_weekdays
from tools.leave_drafts import load_draft, validate_update, merge_draft, missing_question
from tools.operations import OperationError
from tools import attendance_chat
from tools import alerts
from tools import academic_guidance
from tools import topic_research


def supervisor_intake_node(state):
    state['current_datetime'] = college_now().isoformat()
    history = state.get('chat_history', [])
    state['last_timetable_context'] = next((message['timetable_context'] for message in reversed(history)
        if message.get('role') == 'assistant' and not message.get('failed') and isinstance(message.get('timetable_context'), dict)), None)
    # Keep the stored transcript intact; transport failures are not answers or topic changes.
    state['chat_history'] = [message for message in history if not (message.get('role') == 'assistant' and message.get('failed'))]
    state["agent_trace"].append("supervisor_intake")
    return state


def supervisor_node(state):
    attendance_courses, attendance_draft, attendance_revision = attendance_chat.context(state['user_id'],state['role'],state['session_id'])
    draft, revision = load_draft(state['user_id'],state['session_id']) if state['role']=='student' else (None,0)
    modules = state.get('modules')
    state['timetable_catalog'] = timetable_catalog(state['user_id'], state['role']) if modules is None or 'timetable' in modules else None
    guidance_catalog = None
    if modules is None or set(modules).intersection({'academic_guidance','research'}):
        try:
            guidance_catalog = academic_guidance.interpretation_context(state['user_id'], state['role'])
        except Exception:
            pass
    alert_context = alerts.interpretation_context(state['user_id'], state['role']) if modules is None or set(modules).intersection({'attendance','classrooms','mess','leave','timetable','courses'}) else None
    # A short answer to the immediately preceding purpose question continues that
    # booking. Keep the original query/history for validation and authorization.
    history = state.get('chat_history', [])
    previous = history[-1] if history else {}
    purpose = None
    if state['role'] == 'faculty' and previous.get('role') == 'assistant' and not previous.get('actions'):
        question = previous.get('content', '')
        pending = re.search(r'\bpurpose\b', question, re.I) and re.search(r'\b(book|booking|classroom)\b', question, re.I) and '?' in question
        explicit = re.fullmatch(r'\s*(?:purpose\s*[:=-]\s*|the purpose is\s+)(.+)', state['query'], re.I | re.S)
        if pending and explicit and explicit.group(1).strip():
            purpose = explicit.group(1).strip()
    continuation = None
    if purpose:
        continuation = ('Continue the pending classroom booking requested in this session. '
                        f'The user has now supplied the purpose: {purpose}. '
                        'Resolve the selected room, date, times and requirements from user history. '
                        'Ask only for fields still missing; the purpose is no longer missing.')
    try:
        catalog = menu_catalog(state['user_id'], state['role'])
    except Exception:
        catalog = None
    try:
        state['transport_catalog'] = transport_catalog(state['user_id'], state['role'])
    except Exception:
        state['transport_catalog'] = None
    feedback = None
    relative_weekdays = {label: (date.fromisoformat(state['current_date']) + timedelta(days=offset)).weekday()
                         for label, offset in [('today', 0), ('tomorrow', 1), ('yesterday', -1)]}
    for attempt in range(3):
        try:
            result = llm.invoke_json(scoped_prompt(SUPERVISOR_AGENT_PROMPT, state), scoped_payload({
                "user_role": state["role"], "user_id": state["user_id"], "query": state["query"],
                "chat_history": state["chat_history"], "database_schema": DATABASE_SCHEMA,
                "capability_matrix": ROLE_CAPABILITY_MATRIX, "current_date": state["current_date"],
                "timezone": state["timezone"], "booking_continuation": continuation,
                "current_datetime": state.get('current_datetime'),
                "menu_catalog": catalog, "validation_feedback": feedback,
                "transport_catalog": state.get('transport_catalog'),
                "relative_weekdays": relative_weekdays,
                "active_leave_draft": draft,
                "attendance_courses": attendance_courses, "active_attendance_draft": attendance_draft,
                "alert_context": alert_context,
                "academic_guidance_catalog": guidance_catalog,
                "timetable_catalog": state.get('timetable_catalog'),
                "last_timetable_context": state.get('last_timetable_context'),
            }, state))
            route = result.get('route')
            if route not in {'sql','rag','both','direct','menu'}:
                raise ValueError('Supervisor returned an invalid route.')
            for key in ('scoped_request','retrieval_query','direct_answer'):
                if result.get(key) is not None and not isinstance(result[key],str):
                    raise ValueError('Supervisor returned an invalid text field.')
            request = result.get('menu_request')
            timetable = result.get('timetable_request')
            teaching_load = result.get('teaching_load_request')
            leave_update = result.get('leave_update')
            attendance_update = result.get('attendance_update')
            eligibility_request = result.get('course_eligibility_request')
            alert_update = result.get('alert_update')
            guidance_request = result.get('academic_guidance_request')
            research_request = result.get('research_request')
            if research_request is not None:
                topic_research.validate(research_request)
                if route != 'direct' or any(result.get(k) is not None for k in ('sql','sql_query','operation','academic_guidance_request','menu_request','timetable_request','leave_update','attendance_update','alert_update','course_eligibility_request','teaching_load_request')):
                    raise ValueError('Research emails use a separate structured research_request without SQL or other actions.')
            elif modules and 'research' in modules and result.get('needs_clarification') is not True:
                raise ValueError('On-demand research requires research_request or a specific clarification; do not claim it was emailed.')
            if modules and 'timetable' in modules and route == 'direct' and not any(
                    value is not None for value in (timetable, teaching_load, alert_update, attendance_update, leave_update, guidance_request)):
                clarification = result.get('needs_clarification', False)
                if clarification is not True:
                    raise ValueError('Timetable questions and contextual weekday requests require timetable_request and a fresh lookup. Reuse the resolved course/date context when appropriate; never substitute today\'s weekday. For genuinely ambiguous intent only, return needs_clarification=true with a specific direct_answer question.')
            if guidance_request is not None:
                academic_guidance.validate_request(guidance_request)
                if route != 'direct' or any(result.get(k) is not None for k in ('sql', 'sql_query', 'operation', 'menu_request', 'timetable_request', 'leave_update', 'attendance_update', 'course_eligibility_request', 'teaching_load_request', 'alert_update')):
                    raise ValueError('Handle academic guidance as a separate trusted read without SQL or actions.')
            elif modules and 'academic_guidance' in modules and research_request is None:
                raise ValueError('Academic guidance requires academic_guidance_request, including with multiple modules. Do not answer directly or claim data is unavailable before lookup. Supply the intended mode and known fields; leave missing course/topic fields null for the trusted resolver to clarify.')
            if alert_update is not None:
                if route != 'direct' or any(value is not None for value in (attendance_update, leave_update, timetable, request, eligibility_request, teaching_load)) or any(result.get(key) is not None for key in ('sql','sql_query','operation')):
                    raise ValueError('Handle alert changes separately from other tasks.')
                if not isinstance(alert_update, dict):
                    raise ValueError('Alert request must be structured.')
            if eligibility_request is not None:
                if eligibility_request != {"action": "list_eligible"}:
                    raise ValueError('Course eligibility requests must use the supported list_eligible action.')
                if state['role'] != 'student':
                    raise ValueError('The course eligibility lookup is for students only.')
                if route != 'sql' or any(result.get(k) is not None for k in ('sql', 'sql_query', 'operation', 'menu_request', 'timetable_request', 'leave_update', 'attendance_update')):
                    raise ValueError('Handle course eligibility as a separate read request.')
            if teaching_load is not None:
                if state['role'] != 'faculty':
                    raise ValueError('Teaching-load lookup is available to faculty only.')
                if route != 'sql' or any(result.get(k) is not None for k in ('sql', 'sql_query', 'operation', 'menu_request', 'timetable_request', 'leave_update', 'attendance_update', 'course_eligibility_request')):
                    raise ValueError('Handle teaching load as a separate read request.')
                validate_teaching_load_request(teaching_load, datetime.fromisoformat(state['current_datetime']) if state.get('current_datetime') else None)
            if attendance_update is not None:
                if state['role']!='faculty':
                    state.update(route='direct',direct_answer='Only teaching faculty can mark attendance.')
                    return state
                attendance_chat.validate(attendance_update)
                if any(v is not None for v in (leave_update,timetable,request)):
                    raise ValueError('Handle attendance marking separately from other tasks. Ask which task to perform first.')
            if leave_update is not None:
                if state['role']!='student':
                    raise ValueError('Only students can prepare leave submissions.')
                validate_update(leave_update)
                if timetable is not None or request is not None:
                    raise ValueError('Resolve the leave draft separately from timetable or menu requests; ask which task to handle first if ambiguous.')
            if timetable is not None:
                if route not in {'sql', 'both'} or any(result.get(k) is not None for k in ('sql', 'sql_query', 'operation')):
                    raise ValueError('Timetable requests use route sql or both with structured fields, never generated SQL or operations.')
                validate_timetable_request(timetable)
            if route == 'menu' and request is None:
                raise ValueError('Menu route requires a structured menu_request.')
            if request is not None:
                if any(key in result for key in ('sql','sql_query','table','columns')):
                    raise ValueError('SQL and database identifiers are not accepted for menu requests.')
                if catalog is None:
                    state['menu_error'] = 'The menu information is temporarily unavailable. Please try again.'
                else:
                    try:
                        state['menu_request'] = validate_menu_request(request, catalog, relative_weekdays)
                    except UnsupportedMenuChoice as exc:
                        state['menu_error'] = str(exc)
            break
        except (ValueError, TypeError) as exc:
            feedback = str(exc)
    else:
        state.update(route='direct', direct_answer="I couldn't interpret that request reliably. Please clarify the details you want to use.")
        state['agent_trace'].append('supervisor')
        return state
    state.update(route=route, scoped_request=result.get('scoped_request') or state['query'],
                 retrieval_query=result.get('retrieval_query') or state['query'],
                 direct_answer=result.get('direct_answer') or 'Please clarify your college question.')
    if research_request is not None:
        try:
            outcome = topic_research.enqueue_request(state['user_id'],state['role'],state['session_id'],
                research_request,state.get('request_id'),state['query'])
            state.update(route='direct',final_response=outcome['response'],actions=outcome['actions'],research_request=research_request)
        except (OperationError,PermissionError,ValueError) as exc:
            state.update(route='direct',final_response=str(exc))
        state['agent_trace'].append('topic_research_queue')
        return state
    if eligibility_request is not None:
        state['course_eligibility_request'] = eligibility_request
    if guidance_request is not None:
        state['academic_guidance_request'] = guidance_request
    if teaching_load is not None:
        state['teaching_load_request'] = teaching_load
    if alert_update is not None:
        try:
            outcome=alerts.chat_update(state['user_id'],state['role'],alert_update,state['session_id'])
            state.update(route='direct',final_response=outcome['response'],actions=outcome['actions'],alert_update=alert_update)
            if outcome.get('confirmation'):
                state['confirmation']=outcome['confirmation']
        except (OperationError,PermissionError,ValueError) as exc:
            state.update(route='direct',final_response=str(exc))
        state['agent_trace'].append('alert_tool')
        return state
    if timetable is not None:
        state['timetable_request'] = timetable
    if attendance_update is not None:
        try:
            outcome=attendance_chat.handle(state['user_id'],state['role'],state['session_id'],attendance_update,attendance_revision,state.get('request_id'))
            state.update(route='direct',final_response=outcome['response'],actions=outcome['actions'])
        except (OperationError,PermissionError,ValueError) as exc:
            state.update(route='direct',final_response=str(exc))
        state['agent_trace'].append('attendance_tool')
        return state
    elif attendance_draft is not None:
        attendance_chat.handle(state['user_id'],state['role'],state['session_id'],{'action':'cancel'},attendance_revision,state.get('request_id'))
    if leave_update is not None:
        try:
            merged = merge_draft(state['user_id'],state['role'],state['session_id'],leave_update,revision)
            if leave_update['action']=='cancel':
                state.update(route='direct',direct_answer='Leave draft cancelled. Nothing was submitted.')
            else:
                question = missing_question(merged['draft'])
                if question:
                    state.update(route='direct',direct_answer=question)
                else:
                    state.update(route='sql',operation={'type':'submit_leave',**merged['draft']},
                                 leave_draft_ready=True,leave_draft_revision=merged['revision'])
        except (OperationError,PermissionError,ValueError) as exc:
            state.update(route='direct',direct_answer=str(exc))
    elif draft is not None:
        # A different task ends the incomplete draft; do not reuse its reason later.
        try:
            merge_draft(state['user_id'],state['role'],state['session_id'],{'action':'cancel'},revision)
        except (OperationError,PermissionError,ValueError):
            pass
    if continuation:
        state.update(route='sql', scoped_request=continuation)
    state["agent_trace"].append("supervisor")
    return state


def _projection_text(result):
    if result.get('mode') == 'class_count':
        name = f"{result['course_name']} ({result['course_id']})"
        if result['projected_percentage'] is None:
            return f"Attendance projection for {name} is unavailable because attendance counts are missing or invalid."
        return (f"Your current attendance in {name} is {result['current_percentage']:.2f}% "
                f"({result['attended_classes']}/{result['total_classes']}).\n"
                f"If you miss the next {result['missed_classes']} classes, it will be "
                f"{result['projected_percentage']:.2f}% "
                f"({result['attended_classes']}/{result['total_classes'] + result['missed_classes']}).\n"
                + result['assumption'])
    dates = result['start_date'] if result['start_date']==result['end_date'] else result['start_date'] + ' to ' + result['end_date']
    lines = [f"Attendance if absent on {dates} ({result['timezone']}):"]
    overall = result['overall']
    current = overall['current_percentage']
    projected = overall['projected_percentage']
    lines.append('Current overall attendance: ' + (f'{current:.2f}%' if current is not None else 'unavailable.'))
    lines.append('Projected overall attendance: ' + (f'{projected:.2f}%' if projected is not None else 'unavailable.'))
    if projected is not None:
        lines.append(f"Calculation: {overall['attended_classes']} / ({overall['total_classes']} + {overall['missed_classes']}) x 100 = {projected:.2f}%.")
    if not result['courses']:
        lines.append('No currently enrolled courses were found.')
    for row in result['courses']:
        if row['unavailable_reason']:
            lines.append(f"- {row['course_name']}: {row['unavailable_reason']}.")
        elif result['include_courses']:
            lines.append(f"- {row['course_name']}: {row['attended_classes']} / ({row['total_classes']} + {row['missed_classes']}) x 100 = {row['projected_percentage']:.2f}% (current {row['current_percentage']:.2f}%).")
    lines.append(result['assumption'])
    return '\n'.join(lines)


def _clean_source_commentary(answer, sources):
    # Only remove known citation markers and standalone provenance sentences.
    answer = re.sub(r'(?im)^\s*[*_]*(?:this (?:figure|information|answer|fare|schedule)|these (?:details|results|data|answers))\b[^\n]*(?:\bfrom\b|\bbased on\b)[^\n]*(?:\.md\b|\bdatabase\b|\bknowledge base\b)[^\n]*$', '', answer)
    for source in sources:
        answer = answer.replace(f'[{source}]', '')
        answer = re.sub(r'(?i)\b(?:according to|as (?:listed|stated|shown) in|from)\s+(?:the\s+)?[`*]*' + re.escape(source) + r'[`*]*(?:\s+file)?\s*[,;:]?\s*', '', answer)
        answer = answer.replace(source, '')
    answer = re.sub(r'(?im)^\s*(?:Sources?|References):[^\n]*$', '', answer)
    answer = re.sub(r'(?i)\bThese (?:details|results|data|answers) are (?:taken|obtained|retrieved) (?:directly )?from the database query results?\.?', '', answer)
    answer = re.sub(r'(?im)^\s*[*_]*(?:\(fictional demo\.?\)|fictional demo\.?|demo information\.?)[*_]*\s*$', '', answer)
    return answer.strip()


def supervisor_aggregate_node(state):
    result = state.get("sql_result")
    if state.get("final_response"):
        answer = state["final_response"]
    elif state.get('academic_guidance_result') is not None:
        evidence = state['academic_guidance_result']
        try:
            payload = {
                'query': state['query'], 'resolved_request': state['academic_guidance_request'],
                'evidence': evidence}
            if evidence.get('clarification') or evidence.get('missing'):
                answer = academic_guidance.factual_response(evidence)
            elif evidence['mode'] == 'focused':
                selection = llm.invoke_json(academic_guidance.FOCUSED_INSTRUCTIONS, payload)
                answer = academic_guidance.render_focused(selection, evidence)
            elif evidence['mode'] == 'explain':
                selection = llm.invoke_json(academic_guidance.EXPLANATION_INSTRUCTIONS, payload)
                answer = academic_guidance.render_explanation(selection, evidence)
            else:
                answer = llm.invoke_text(academic_guidance.ANSWER_INSTRUCTIONS, payload)
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError('Empty academic answer')
        except Exception:
            answer = academic_guidance.factual_response(evidence)
            state['agent_trace'].append('academic_guidance_fallback')
    elif state.get('teaching_load_result') is not None:
        answer = format_teaching_load(state['teaching_load_result'])
    elif state.get('course_eligibility_result') is not None:
        evidence = state['course_eligibility_result']
        courses = evidence['courses']
        if courses:
            answer = f"Courses you can register for in {evidence['department']}:\n" + '\n'.join(
                f"- {course['course_id']} — {course['course_name']}" for course in courses
            )
        elif evidence['unregistered_count'] == 0:
            answer = f"There are no unregistered courses listed for your {evidence['department']} department right now."
        else:
            answer = (f"There are no unregistered {evidence['department']} courses that meet the "
                      "recorded prerequisite requirements right now.")
    elif state["route"] == "direct":
        answer = state["direct_answer"]
    elif state.get("validation_verdict") == "invalid" and state["route"] in {"sql", "both"}:
        answer = "I couldn't safely complete the database request after three attempts. Please rephrase it."
    elif isinstance(result, dict) and result.get('kind') == 'timetable':
        try:
            weekday_rule = '\nAlways include the supplied weekday beside each session date, including a Day column in tables. Use the recorded date-to-weekday mapping, never guess from today. Use natural singular/plural wording: 1 class scheduled; 2 classes scheduled. Never write class(es). Use recorded counts only when explaining incomplete coverage.\n'
            answer = llm.invoke_text(scoped_prompt(ANSWER_PROMPT, state) + weekday_rule + '\nExplain only the supplied timetable evidence. Show resolved dates, class counts, course IDs/names, times and faculty. NULL count means unknown, never zero. Distinguish ongoing sessions from next_sessions. Missing coverage means the next recorded class is not guaranteed to be the actual next class. Do not invent rooms, holidays, cancellations or classes. For next mode state the seven-calendar-day window. For mixed policy questions use only the supplied policy context. When selected_courses is non-null, discuss only those courses. For filtered week/range answers use one compact table of matching sessions and their total; omit empty-day sections but explicitly report missing coverage.', scoped_payload({
                'query': state['query'], 'timetable': result,
                'context': re.sub(r'(?m)^\[[^\]\n]+\.md\]\s*', '', state.get('rag_context', '')),
            }, state), temperature=0.0)
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError('Empty timetable answer')
            displayed = result['ongoing'] + result['next_sessions'] if result['mode']=='next' else [r for d in result['days'] for r in d['sessions']]
            required = [result['start_date'], result['end_date'], result['timezone']]
            required += [str(r[k]) for r in displayed for k in ('course_id','course_name','start_time','end_time','faculty_name') if r[k]]
            if 'class(es)' in answer.casefold() or any(token not in answer for token in required) or any(d['count'] is None for d in result['days']) or not valid_timetable_weekdays(answer, displayed):
                answer = format_timetable(result)
        except Exception:
            answer = format_timetable(result)
    elif isinstance(result, dict) and result.get("action"):
        answer = f"Done. {result['action'].replace('_', ' ').capitalize()} saved successfully (record {result['record_id']})."
        if result["action"] == "delete_leave":
            answer = f"Leave request #{result['record_id']} deleted."
        if state["route"] == "both":
            try:
                answer += "\n" + llm.invoke_text(scoped_prompt(ANSWER_PROMPT, state), scoped_payload({"query": state["query"], "sql_result": result,
                    "context": re.sub(r"(?m)^\[[^\]\n]+\.md\]\s*", "", state.get("rag_context", "")),
                    "chat_history": state["chat_history"]}, state), temperature=0.0)
            except Exception:
                answer += "\nPolicy interpretation is temporarily unavailable; the operation was saved."
    elif state['route'] == 'menu':
        answer = ''
    else:
        fallback = _projection_text(result) if isinstance(result, dict) and ("projection_date" in result or result.get('mode') == 'class_count') else format_sql_response(result)
        if state.get("projection_date") or state.get("projection_request"):
            answer = fallback
            if state['route'] == 'both':
                try:
                    policy = llm.invoke_text(scoped_prompt(ANSWER_PROMPT, state) + '\nThe application already displays the computed attendance projection. Answer only the policy portion of the question. Do not repeat or recalculate attendance figures.', scoped_payload({
                        'query': state['query'], 'sql_result': result,
                        'context': re.sub(r'(?m)^\[[^\]\n]+\.md\]\s*', '', state.get('rag_context', '')),
                    }, state), temperature=0.0)
                    answer += '\n\n' + policy
                except Exception:
                    answer += '\nPolicy interpretation is temporarily unavailable.'
        else:
            try:
                answer = llm.invoke_text(scoped_prompt(ANSWER_PROMPT, state), scoped_payload({
                    "query": state["query"], "scoped_request": state.get("scoped_request"),
                    "sql_result": result, "context": re.sub(r"(?m)^\[[^\]\n]+\.md\]\s*", "", state.get("rag_context", "")),
                    "chat_history": state["chat_history"],
                    "current_date": state['current_date'], "timezone": state['timezone'],
                }, state), temperature=0.0)
            except Exception:
                answer = fallback or "The policy answer is temporarily unavailable. Please try again."
                if state["route"] == "both":
                    answer += "\nPolicy interpretation is temporarily unavailable."
    if state.get('menu_error'):
        answer = (answer + '\n\n' + state['menu_error']).strip()
    elif state.get('menu_result') is not None:
        evidence = state['menu_result']
        try:
            menu_answer = llm.invoke_text(MENU_ANSWER_PROMPT, scoped_payload({
                'request': evidence['request'], 'rows': evidence['rows'], 'missing': evidence['missing'],
            }, state), temperature=0.0)
            if not isinstance(menu_answer, str) or not menu_answer.strip():
                raise ValueError('Empty menu answer')
        except Exception:
            menu_answer = format_menu_response(evidence['rows'], evidence['request'], evidence['missing'])
        answer = (answer + '\n\n' + menu_answer).strip()
    answer = _clean_source_commentary(answer, state.get('sources', []))
    state["final_response"] = answer
    state["agent_trace"].append("supervisor")
    return state
