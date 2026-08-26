# Collage_Multi_agent_chatbot

A full-stack College Multi-Agent Assistant built with React, Flask, SQLite, LangGraph, Groq LLMs, and RAG. It provides secure role-based chat for students and faculty to access attendance, courses, results, leave requests, classroom information, and college policies through a supervised multi-agent workflow.

## Stack

- React + Vite frontend
- Flask backend with JWT auth
- SQLite seeded college database
- LangGraph orchestration
- Groq LLM calls through `langchain_groq.ChatGroq`
- ChromaDB + `sentence-transformers/all-MiniLM-L6-v2` RAG over local markdown files
- Per-user JSON chat memory

## Setup

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
python backend\db\init_db.py
python backend\app.py
```

In another terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

Or start both development servers from the project root:

```powershell
.\scripts\start-dev.ps1
```

## LangGraph Visualization

<img width="975" height="659" alt="image" src="https://github.com/user-attachments/assets/75bef96f-6b5a-495e-aae5-cbdf03902a23" />


## Demo Accounts

- Student: `192125022`
- Faculty: `FAC001`
- Password for all seeded users: `password123`

## API

- `POST /api/auth/login`
- `GET /api/auth/me`
- `POST /api/chat`
- `GET /api/chat/history?session_id=session_001`

## Notes

Set `GROQ_API_KEY` in `.env` before using `/api/chat`. Agent routing, guardrails, SQL
generation, validation, and answer aggregation are implemented as prompt-backed LLM calls.
The default configured Groq model is `openai/gpt-oss-20b`.
`tools/db_tool.py` adds an infrastructure safety net that blocks multi-statement SQL,
administrative SQL, attendance writes, and unscoped student queries.

For fully embedded RAG, keep `RAG_USE_CHROMA=1` and `ALLOW_MODEL_DOWNLOAD=1` in `.env` on a
machine that can download `sentence-transformers/all-MiniLM-L6-v2`. Without those flags,
the backend uses the same markdown knowledge base with a local lexical fallback so startup
does not hang in offline environments.
