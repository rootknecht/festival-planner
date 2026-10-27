# Festival Planner

A self-hosted, mobile-first web application for organising a festival trip with
a group of friends: who is coming, who drives whom, who brings what, what the
group still has to decide, and everything else discussed — in one place.

Status: **early development** (v0.0.x). Not yet deployable.

## What it does

- Every festival has its own space, visible only to invited people.
- Invite-only accounts — there is no public signup route.
- Dashboard with personal notification blocks: you were mentioned, a poll is
  waiting for your vote, or something changed that names you.
- Car-sharing: cars with seats, drag-and-drop passengers, departure address and
  pickup stops on an OpenStreetMap view.
- "Who brings what" table: participants as columns, items as rows, amounts with
  units and a per-unit total.
- Polls, a group chat room and a forum, all with `@mentions`.
- Installable as a mobile web app (PWA) with push notifications.
- Themeable: a theme is a single CSS file. Ships with Metal (dark), Matrix and
  Light. German and English.

## Design principles

- One application, not a stack of glued-together services: a single auth
  system, a single place where "who may see this festival" is enforced.
- Server-rendered HTML with HTMX. No SPA framework, no build pipeline, no
  `node_modules` — the page shipped is the page the browser runs.
- Security first: argon2id passwords, server-side sessions, CSRF on every
  state-changing request, strict CSP, no uploads, no outbound fetch of
  user-supplied URLs.
- No tracking, no third-party CDN, no analytics.

## Tech stack

Python 3.12+, FastAPI, Jinja2, HTMX, SQLAlchemy 2 + Alembic, SQLite (WAL) with
PostgreSQL as a drop-in alternative, WebSockets for live updates, Babel for
translations.

## Development

    python3 -m venv .venv
    .venv/bin/pip install -e '.[dev]'
    .venv/bin/pytest
    .venv/bin/uvicorn app.main:app --reload

## Licence

AGPL-3.0-or-later. See `LICENSE`.
