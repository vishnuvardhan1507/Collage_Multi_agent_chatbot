# Automatic subject-update emails

Every student automatically receives major updates relevant to their entire recorded curriculum
(completed, enrolled and pending courses). There is no subscription, opt-out rule, course selector,
or editable research alert. Other personal alerts retain their existing behavior. Faculty do not
receive these student digests. Chat explains the service; it cannot create/delete a subscription.

## Collection and screening

The notification worker checks daily from 06:00 Asia/Kolkata. Its separate collection thread makes
bounded HTTPS requests to official pages/APIs; no open browser window is needed. This is not an
unrestricted web crawler. The research venue allowlist remains in RESEARCH_SOURCES in db/init_db.py.
Individual venues require verified identity and accessible review policy. Research requires original
published reviewed findings plus verified official recognition, such as an award or publisher highlight.
A new ACADEMIC_NOTICE_SOURCES allowlist starts with the official AICTE model-curriculum page.
Official announcements are screened separately for substantial new subject-content recommendations.
No JNTUH affiliation, Telangana board, degree regulation or institution-specific syllabus adoption is assumed.
General recommendations are explicitly distinguished from changes to students' prescribed syllabi.

Local all-MiniLM-L6-v2 embeddings identify related recorded syllabus courses. The LLM assesses compact
source evidence, and trusted code validates course references, dates, URLs and supporting verbatim quotes.
The rolling publication window is 30 days. Undated listings, minor changes, unrelated information,
preprints, retractions, inaccessible pages and unsupported claims do not qualify. No fabricated updates
are introduced to make a demonstration produce email. No private student information leaves in requests.

Requests use approved hosts, checked redirects, bounded timeouts/retries and a 1.5 MB response cap.
PDF text extraction uses pypdf (first 30 pages, capped text); inaccessible/encrypted/undated documents
are skipped. Pages are cached for 24 hours; qualified publications/notices are rechecked daily.
Collection uses persisted leases/checkpoints, at most one research item and one official notice per pass,
with a 60-second interval. A failed paper no longer aborts the remaining journal records. Crossref http
publisher links are upgraded to HTTPS only on approved hosts. Blocked sites are never bypassed.
Source failures and complete_with_errors are recorded explicitly; completed traversal does not imply
successful collection or any qualifying findings. Discovery is bounded, not an exhaustive coverage guarantee.

## Delivery and history

Daily digests are queued as qualifying records become available after 06:00 IST. There is at most one
digest per student/date with at most three new items. Empty days produce no email. Additional undelivered
items remain eligible for later daily digests while current and verified; missed days are not backfilled.
Each item includes subject IDs, publication date, findings/change, significance, limitations and direct links.
The current curriculum, source enablement and qualification freshness are checked again before delivery.
Stale, removed or inapplicable evidence is suppressed. Completed courses remain part of the curriculum.

Daily unique keys and canonical publication IDs prevent duplicates across courses and worker retries.
Previously delivered legacy weekly publications are excluded. SMTP retries reuse a stable Message-ID;
SMTP cannot guarantee exactly-once arrival after ambiguous network failures. SMTP acceptance is recorded
as sent; this does not prove inbox delivery or that a student read the email.

Migration 19 adds subject_update_digests, academic_notice_sources, academic_notices and
academic_notice_candidates. All are private and excluded from generated SQL. Legacy research rules are
archived and hidden, not deleted; delivered history is preserved. Pending legacy weekly messages are
suppressed and are replaced by the new automatic eligibility path. Current system notifications appear
alongside personal alert activity in the existing latest-five Recent notifications with IST timestamps.
System messages are not tied to a deletable rule. Other alert and academic data are unchanged.

## Run and diagnose

From the project root:

```powershell
.venv\Scripts\python.exe backend\db\migrate.py
Set-Location backend
..\.venv\Scripts\python.exe -m flask --app app:create_app notification-worker
```

Run only one worker; restart it and the backend after code updates. scripts/start-dev.ps1 also starts
it. --once drains currently due mail without starting source collection. Keep the computer and worker
running for automatic checks/delivery. Monitor research_checkpoints (collection and notices plus
source-specific outcomes), research_publications, academic_notices and notification_outbox through an
operator connection. No public administrative endpoint or generated SQL access is added.

## Verification and rollback

Tests use isolated databases, mocked publisher/official pages, LLM responses and SMTP. They cover
no-subscription delivery, curriculum scope, evidence grounding, migration preservation, daily limits,
deduplication, owner isolation, revalidation and SMTP retry behavior. Browser checks use mocked APIs.

Backups and restore instructions for this change are in
backups/automatic-subject-updates-20261117-113125/. Stop the backend and worker before rollback,
compare post-change hashes to preserve subsequent edits, restore only backed-up files, and suppress
pending/failed subject-update:% messages before running old code. Keep the added private tables and
archived delivery history. Do not restore the old database over later academic or conversation changes.
