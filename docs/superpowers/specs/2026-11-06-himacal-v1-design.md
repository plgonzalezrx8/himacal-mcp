# Himacal-MCP v1 — approved design

The user approved implementation on 2026-11-06 in this chat. This document records that contract.

Historical design: on 2026-11-07, the user rejected the IMAP reset race described below. The shipped implementation now patches the pinned Himalaya CLI to validate UIDVALIDITY on the same connection before message operations; see [the guard documentation](../../../patches/README.md). This supersedes the unmodified-CLI requirement and accepted timing limitation below.

## Architecture and configuration

One Docker-first Python 3.13 service owns a Streamable HTTP MCP endpoint at `/mcp`.
Use official `mcp==2.3.0`, `caldav==3.7.0`, `icalendar==7.3.0`, locked dependencies,
and an unmodified Himalaya v2.2.1 CLI. Never run or proxy Himalaya's own MCP server.
The chosen CLI approach avoids rewriting its protocol implementation; direct Rust
integration and a patched CLI were considered and rejected in favor of upstream reuse.

Root Docker Compose and Dockerfile build and run a non-root service on port 8000,
published to localhost by default. An existing reverse proxy supplies HTTPS.
Read-only TOML contains named accounts with native Himalaya email settings and/or
CalDAV URL, username, password-file and timezone. Compose secrets supply credentials;
the shared MCP bearer password is independent of account passwords. No secrets in
images, Git, tool results or logs. Invalid/missing required setup fails startup.
Document start/stop, account removal, token rotation and reverse-proxy configuration.

## Interface and safety

One shared password authorizes all configured accounts. Every application route,
including health, validates its Bearer header in constant time. No OAuth login flow.
Validate Host/Origin and enforce a 40 MiB request limit. Current and legacy Streamable
HTTP clients must work via SDK support. Valid calls execute directly; client owns
approval. No application-level preview/confirmation workflow.

Email tools list accounts/mailboxes/messages, search/read messages, send/reply/forward,
move/copy/trash, add/remove safe flags, and list/get/send attachments. Reads do not mark
seen. Trash is reversible using move; no permanent expunge or destructive deletion flags.
Attachments use filename/MIME/base64 (10 MiB each, 25 MiB aggregate), with private
temporary files and no arbitrary filesystem access. Forward preserves attachments.
Message references include account/mailbox/ID and IMAP UIDVALIDITY. Detect stale refs;
the user accepted that standard CLI checks cannot eliminate resets between check/action.
Use fixed argument arrays, explicit account/config, bounded calls, safe errors, no shell
or generic passthrough. Never retry mutations automatically; possible acceptance followed
by failures (including failed Sent-copy saving) means unknown outcome, not safe-to-retry.

Calendar tools list/search/get/create/update/delete timed/all-day events, recurring series
and individual occurrences. Adapt the supplied `icloud_cal.py`, replacing CLI prints and
SystemExit with structured results/errors and generic configuration. Resolve a discovered
calendar plus UID; never arbitrary URL deletion or first-calendar fallback. Preserve
full VCALENDAR/VTIMEZONE and unrelated fields. Updates/deletes require strong ETags and
conditional writes; stale or missing versions fail safely. Recurring mutations require
explicit series/occurrence scope and original RECURRENCE-ID. Occurrence deletion is
cancellation, reversible by update. Validate dates, timezones/DST and recurrence via
libraries, bound search/expansion. Invitations/RSVP and scheduling-triggering writes are
out; scheduling events remain readable. Full-event deletion and sending have no reliable
undo and must be described as such.

## Acceptance and exclusions

Unit/HTTP tests: auth on every route and MCP method, invalid tokens/origins, rotation,
configuration/account routing, structured errors/redaction, safe subprocess arguments,
attachment size/temp handling and unknown mutation outcomes. Integration: actual pinned
Himalaya against disposable GreenMail and CalDAV against disposable Radicale, two
accounts, all mail flows, attachments, read flags, trash/restore, stale references;
calendar dates/DST, recurrence exceptions, occurrence cancellation/restoration, series,
unrelated properties, ETag conflict and foreign-calendar rejection. Root Compose builds,
becomes healthy, accepts authenticated MCP and survives restart. Document tested versus
expected provider compatibility.

No live iCloud actions, remote deployment/publication, OAuth, UI, database, background
sync, invitations/RSVP, mailbox/calendar collection CRUD or CLI source patching.
