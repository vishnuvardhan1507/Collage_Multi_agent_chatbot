# On-demand student topic research

Students can ask: "Research SQL joins and email me useful examples." The LLM resolves the course,
topic and learning goal using conversation context. Ordinary tutoring stays in academic guidance;
requests that do not make email intent clear receive a clarification. Faculty cannot queue these jobs.

## Data flow

Guardrail selects the research module. Supervisor emits research_request with course, topic, goal.
Trusted code validates fields, resolves course aliases within the student's entire recorded curriculum,
and uses an LLM to select valid parent-topic references. Missing/ambiguous details ask for clarification.
No generated SQL, user-supplied fetch URLs or alternate email recipients are accepted.

A committed research_jobs row returns a queued acknowledgement, not a sent claim. A dedicated
notification-worker thread checks every five seconds independently of daily-update collection.
The worker claims jobs with a 30-minute lease, runs network/model work outside transactions and commits
results and the email-outbox event atomically. Interpretation failures retry at most three times;
exhaustion produces an honest outcome email. Worker restarts recover expired leases.

Research uses approved no-key documentation indexes (PostgreSQL, Python, scikit-learn, MIT OCW,
OpenStax), plus Crossref discovery restricted to the existing approved publication venues. The LLM
selects at most two indexes and three links per index; up to three eligible publisher pages supplement
them. HTTPS hosts/redirects, size limits, caching and request timeouts reuse the trusted fetcher.
Dynamic, blocked or paywalled sources may not be accessible. This is a bounded approved-source search,
not an exhaustive general search engine or a guarantee that every subject has accessible references.

Unlike automatic major-update digests, learning references need not be recent breakthroughs.
A final LLM assessment selects up to five verified links in reading order. Trusted code checks every
URL, page title and supporting quotation against fetched evidence. Website instructions are untrusted;
educational examples are never executed. Pages are summarized from compact excerpts to limit tokens.
Source errors are recorded by exception type; student IDs and email addresses are not included in
source-discovery payloads. Relevance calls receive course/topic context, not private academic records.

## Delivery and user experience

Emails include topic, course ID, reference titles, source names, concise usefulness summaries and direct
links. Multipart emails contain escaped HTML anchors and a plain-text fallback. Only the account's
registered email is used. Curriculum membership and account role are rechecked before SMTP delivery.
Recent notifications shows queued/running jobs and email outcomes, scoped to the account and limited
to the latest five entries. Opening Alerts again refreshes status; queueing through chat refreshes its
existing action mechanism. No popup, subscription or public endpoint is added.

Stable event keys prevent duplicate outbox records. Identical resolved research requests within the
same conversation/day reuse the job. Per-request receipts recover saved acknowledgements after a
history-write failure without rerunning research. Deleted conversations are never recreated; clearing
history does not cancel an already authorized email job. A materially refined topic is a new request.
SMTP retries reuse Message-ID; ambiguous SMTP failures cannot guarantee exactly-once inbox arrival.
Sent means SMTP accepted, not that the student read the message.

## Run, verification and rollback

Install backend requirements, run backend/db/migrate.py, and restart the backend and one
notification-worker. Use the existing command from backend:

    ..\.venv\Scripts\python.exe -m flask --app app notification-worker

The --once option drains due outbox mail only; it does not start research threads.
Migration 20 adds private research_jobs and research_job_receipts with backup and repeatability.
They remain excluded from generated SQL. Other alerts, automatic daily updates, records and conversations
are preserved. Automated tests use isolated databases, mocked pages, model decisions and SMTP.

Backups: backups/on-demand-research-20261118-103353. See RESTORE.md for targeted rollback.
Stop the backend and worker before restoring code. Preserve the new private tables and history;
suppress pending topic-research:% emails and unfinished jobs before running old code. Never replace
current academic data with the pre-change database snapshot after subsequent student activity.
