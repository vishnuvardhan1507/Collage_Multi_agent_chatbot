# AI-Powered College Multi-Agent Assistant System

AI college assistant that lets students and faculty ask academic and campus-management questions in natural language. The system combines React, Flask, SQLite, JWT authentication, LangGraph orchestration, Groq LLM calls, and RAG to provide secure role-aware answers for attendance, courses, results, leave requests, classrooms, and college policies.

## Project Overview

Educational institutions manage attendance, courses, results, leave requests, classrooms, and policy information across multiple systems. This project provides a conversational interface that helps authenticated students and faculty access that information quickly while preserving role-based access control.

The assistant supports:

- Student and faculty login
- Natural-language chat for college queries
- Database-backed answers for academic records
- RAG-based answers for handbook, leave policy, bus routes, and infrastructure
- Student leave submission and faculty leave review
- Agent trace display and workflow visualization

## Screenshots

LangGraph workflow:

<img width="975" height="659" alt="LangGraph workflow" src="https://github.com/user-attachments/assets/75bef96f-6b5a-495e-aae5-cbdf03902a23" />

Login Page:

<img width="696" height="399" alt="image" src="https://github.com/user-attachments/assets/2607278e-c257-41ac-9123-012c849a6363" />

Student Chat Interface:

<img width="753" height="432" alt="image" src="https://github.com/user-attachments/assets/5beda4ba-daa8-49f1-a487-63e895eb4e45" />

Faculty Dashboard:

<img width="770" height="401" alt="image" src="https://github.com/user-attachments/assets/587f91eb-b3be-4cea-9dd9-6290bb440d62" />

ER diagram:

<img width="855" height="650" alt="image" src="https://github.com/user-attachments/assets/147efd9e-2749-4f6e-bcf7-9f9527be766e" />

## Technology Stack

- Frontend: React, Vite, Axios, lucide-react
- Backend: Flask, Flask-CORS, Flask-JWT-Extended, Werkzeug
- Agent Framework: LangGraph, LangChain
- LLM Provider: Groq through `langchain_groq.ChatGroq`
- Database: SQLite
- RAG: ChromaDB, sentence-transformers, local markdown knowledge base, lexical fallback
- Storage: Per-user JSON chat memory

## System Workflow

The Supervisor Agent is the central coordinator. It receives the user query, invokes guardrails, decides the route, communicates with specialized agents, and aggregates the final response.

Main flow:

```text
start -> supervisor -> guardrail -> supervisor -> specialized route -> supervisor/aggregate -> memory_write -> end
```

SQL flow:

```text
supervisor -> sql_query_agent -> supervisor -> validator -> supervisor -> execute_sql -> supervisor -> aggregate
```

## Agent Roles

| Agent / Node | Responsibility |
| --- | --- |
| Supervisor Agent | Central coordinator. Receives queries, invokes guardrail, routes work, manages retries, communicates with specialized agents, and aggregates final responses. |
| Guardrail Agent | Screens requests for safety, college scope, role access, prompt injection, impersonation, and unsafe data access. |
| SQL Query Agent | Converts scoped natural-language requests into SQLite queries and revises them using validator feedback. |
| Validator Agent | Checks SQL correctness, schema usage, row-level security, allowed operations, and safety before execution. |
| RAG Tool | Retrieves relevant context from markdown knowledge files or Chroma for policy and campus information. |
| Execute SQL Node | Executes validated SQL through the database tool and returns results to the supervisor. |
| Aggregate Node | Produces the final natural-language chatbot response from direct, RAG, or SQL results. |
| Memory Write Node | Stores user and assistant turns in per-user JSON session memory. |

## Database Design

SQLite stores users, students, faculty, courses, prerequisites, course registrations, attendance, results, leave requests, classrooms, and classroom bookings. The live schema is read from `backend/db/schema.py`.

## Security and Validation

- JWT authentication identifies the active user.
- Role policy limits student and faculty access.
- Guardrails block unrelated, unsafe, impersonation, and prompt-injection requests.
- Validator checks generated SQL before execution.
- Database tool blocks multi-statement SQL, administrative SQL, unsafe writes, attendance modifications, and unscoped student queries.

## Requirements

- Python 3.10+
- Node.js and npm
- Groq API key

## Setup

Create environment and install backend dependencies:

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
python backend\db\init_db.py
```

Update `.env` with your Groq key:

```text
GROQ_API_KEY=your_key_here
GROQ_MODEL=openai/gpt-oss-20b
```

Install frontend dependencies:

```powershell
cd frontend
npm install
```

## Run Locally

Start both backend and frontend:

```powershell
.\scripts\start-dev.ps1
```

Or run separately:

```powershell
python backend\app.py
```

```powershell
cd frontend
npm run dev
```

Open:

```text
http://localhost:5173
```

## Demo Accounts

- Student: `192125022`
- Faculty: `FAC001`
- Password for all seeded users: `password123`

## API Endpoints

- `POST /api/auth/login`
- `GET /api/auth/me`
- `POST /api/chat`
- `GET /api/chat/history?session_id=session_001`
- `GET /api/leaves`
- `POST /api/leaves`
- `PATCH /api/leaves/<leave_id>/review`

## Sample Inputs and Expected Outputs

| Role | Sample Input | Expected Output |
| --- | --- | --- |
| Student | `What is my attendance in Machine Learning?` | Returns the student's scoped attendance details. |
| Student | `Show my enrolled and pending courses.` | Lists only the logged-in student's courses. |
| Student | `What is the minimum attendance policy?` | Answers from handbook/RAG context. |
| Student | `Show attendance of student 192125023.` | Blocks access to another student's data. |
| Faculty | `Which leave requests are pending for my students?` | Lists pending leave requests within faculty scope. |
| Faculty | `Approve leave request 1.` | Updates the request only if it is within scope. |
| Faculty | `Show my assigned classes.` | Lists courses assigned to the logged-in faculty member. |

## Project Documents

- Project report: `docs/collage_AI_chatbotReport.docx`
- Database ER diagram: `docs/database_er_diagram.mmd`
- LangGraph diagram: `backend/agent_graph.mmd`

## Limitations

- Uses seeded demo data instead of a production database.
- LLM routing depends on Groq API availability.
- RAG answers are limited to the local markdown knowledge base.
- Frontend is demonstration-focused.

## Future Enhancements

- Add administrator dashboards and audit logs.
- Expand database and knowledge-base coverage.
- Add automated tests for APIs and agent routes.
- Deploy with a production database and secure secret management.
