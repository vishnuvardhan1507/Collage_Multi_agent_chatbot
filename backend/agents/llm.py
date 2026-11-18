import json
import re
from typing import Any, Dict, List

from flask import current_app
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq


def get_llm(temperature: float = 0.0):
    api_key = current_app.config.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")
    return ChatGroq(
        api_key=api_key,
        model=current_app.config.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
        temperature=temperature,
        timeout=40,
        max_retries=1,
    )


def invoke_text(system_prompt: str, payload: Dict[str, Any], temperature: float = 0.0) -> str:
    messages: List[Any] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=json.dumps(payload, ensure_ascii=True)),
    ]
    response = get_llm(temperature=temperature).invoke(messages)
    if not isinstance(response.content, str) or not response.content.strip():
        raise ValueError("Expected a nonempty text response from the agent")
    return response.content


def invoke_json(system_prompt: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    content = invoke_text(system_prompt, payload)
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            raise
        result = json.loads(match.group(0))
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object from the agent")
    return result
