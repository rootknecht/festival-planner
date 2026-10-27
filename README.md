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

Python 3.11+, FastAPI, Jinja2, HTMX, SQLAlchemy 2 + Alembic, SQLite (WAL) with
PostgreSQL as a drop-in alternative, WebSockets for live updates, Babel for
translations.

## Development

    python3 -m venv .venv
    .venv/bin/pip install -e '.[dev]'
    .venv/bin/pytest
    .venv/bin/uvicorn app.main:app --reload

## Running it yourself

There is deliberately **no public signup route**. The first account is created
on the command line and every later account needs an invitation, so an
instance cannot be joined by a stranger who finds the URL.

    FP_ENVIRONMENT=production \
    FP_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
    FP_BASE_URL=https://your.domain \
    .venv/bin/alembic upgrade head

    .venv/bin/python -m app.cli create-operator --email you@example.org --name You

`create-operator` prompts for the password twice. Do not pass `--password`: it
would land in your shell history and in the process table.

Settings are environment variables prefixed `FP_`. In production the app
refuses to start without `FP_SECRET_KEY`, rather than falling back to a
development default that would make every session cookie forgeable.

Mail is optional. With no `FP_SMTP_HOST` the app still works: every invitation
and password reset also produces a copy-paste link for the admin. Web Push is
optional too, and needs a VAPID key pair from `python -m app.cli generate-vapid`
(`FP_VAPID_PRIVATE_KEY_FILE` takes a path, because a PEM is multi-line and
cannot live in an env file).

## Upgrading a deployment

Releases are tags. An upgrade is: fetch the tag, rebuild the virtualenv, run
migrations, restart.

    git fetch --force --tags
    git checkout v0.1.1
    .venv/bin/pip install .
    .venv/bin/alembic upgrade head
    systemctl restart festival-planner

Two things that are easy to get wrong:

- `--force` on the fetch is not optional. A plain `git fetch --tags` does
  **not** update a tag that moved upstream, so a re-cut release silently
  redeploys the old commit under the new tag's name.
- Generate `FP_SECRET_KEY` and the VAPID pair **once** and keep them. Rotating
  the secret key logs every user out; rotating the VAPID keys silently breaks
  every existing push subscription.

Rolling back is the same procedure with the previous tag — provided the release
you are leaving did not run a destructive migration, so take a copy of the
database file first. For SQLite, `sqlite3 festival.db ".backup copy.db"` gives
a consistent copy while the app is running; plain `cp` does not.

## Licence

AGPL-3.0-or-later. See `LICENSE`.
