from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    user_id: str
    role: str
    session_id: str
    query: str
    request_id: str
    chat_history: list[dict]
    current_date: str
    timezone: str
    guardrail_verdict: str
    guardrail_reason: str
    route: str
    modules: list[str] | None
    scoped_request: str
    retrieval_query: str
    direct_answer: str
    sql_query: str | None
    operation: dict | None
    projection_date: str | None
    projection_request: dict | None
    timetable_request: dict | None
    timetable_catalog: list[dict] | None
    last_timetable_context: dict | None
    teaching_load_request: dict | None
    teaching_load_result: dict | None
    academic_guidance_request: dict | None
    research_request: dict | None
    academic_guidance_result: dict | None
    current_datetime: str
    operation_evidence: dict | None
    compiled_sql: str | None
    parameters: tuple | None
    sql_result: Any
    menu_request: dict | None
    transport_catalog: dict | None
    menu_result: dict | None
    menu_error: str | None
    course_eligibility_request: dict | None
    course_eligibility_result: dict | None
    alert_update: dict | None
    validation_verdict: str
    validation_feedback: str | None
    retry_count: int
    terminal: bool
    rag_context: str
    sources: list[str]
    actions: list[str]
    confirmation: dict | None
    leave_draft_ready: bool
    leave_draft_revision: int
    final_response: str
    agent_trace: list[str]
