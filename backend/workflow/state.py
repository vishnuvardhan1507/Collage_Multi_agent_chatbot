from typing import Any, Dict, List, Optional, TypedDict


class AgentState(TypedDict):
    user_id: str
    role: str
    session_id: str
    query: str
    chat_history: List[Dict[str, str]]

    guardrail_verdict: Optional[str]
    guardrail_reason: Optional[str]

    database_schema: Optional[str]
    route: Optional[str]
    scoped_request: Optional[str]
    direct_answer: Optional[str]
    sql_query: Optional[str]
    sql_result: Optional[Any]
    validation_verdict: Optional[str]
    validation_feedback: Optional[str]
    retry_count: int

    rag_context: Optional[str]

    final_response: Optional[str]
    agent_trace: List[str]
