# Festival Planner

A self-hosted, mobile-first web application for organising a festival trip with
a group of friends: who is coming, who drives whom, who brings what, what the
group still has to decide, and everything else discussed — in one place.

Status: **in use** (v0.9.x). Deployed from a pinned tag; see `CHANGELOG.md`.

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
- Themeable: a theme is a single CSS file plus one entry in `THEMES`. Ships
  with Dark, Metal, Punk, Matrix and Light; the festival picks one and any
  user may override it for themselves. German and English.

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

## Email

The app never depends on email. Every invitation also yields a copy-paste link,
and an admin can issue a password-reset link straight from the dashboard. Mail
is a convenience, and the app is built so that losing it costs nobody access.

Configure it with five values:

    FP_SMTP_HOST, FP_SMTP_PORT, FP_SMTP_USER, FP_SMTP_PASSWORD, FP_SMTP_FROM

and optionally `FP_SMTP_REPLY_TO`. Set that last one whenever `FP_SMTP_FROM` is
a `noreply@` on a domain with no MX records: authenticating a domain for
*sending* does not give it a mailbox, and people do reply to invitations.

### If your host blocks outbound SMTP

Many providers block ports 25, 465 and 587 outbound. Test from the machine that
will actually send, not from your laptop:

    timeout 6 bash -c 'cat < /dev/null > /dev/tcp/smtp.example.org/587' && echo open || echo blocked

If they are blocked, a transactional relay on port 2525 is the usual way out.
Check the relay's own documentation for which ports it offers.

### Traps worth knowing before you debug the wrong thing

- **"Unauthorized IP" arrives AFTER a successful login.** Several relays block
  unknown source IPs by default, and the resulting error reads like a bad
  password. Authorise the sending IP in the relay's security settings — and
  note that a container usually egresses via its host's public address, not
  its own, so check the real outbound IP rather than assuming.
- **Relays may add a `List-Unsubscribe` header to transactional mail.** Gmail
  and Apple Mail render it as an Unsubscribe button, and a recipient who taps
  it on an *invitation* is then suppressed for *password resets* too — the
  relay keeps accepting the message and silently drops it, so the application
  sees success. Removing the header is often a paid-plan feature. This is the
  main reason the copy-paste link exists on every invite; check the relay's
  blocklist when somebody insists a mail never arrived.
- **A multi-line secret cannot live in a `.env` file.** Shell-sourced env files
  are line-based. The VAPID private key is therefore a *file path*
  (`FP_VAPID_PRIVATE_KEY_FILE`), not a value.

## Fonts

Two themes ship a display face for the festival name, self-hosted under
`app/static/fonts/` with its licence beside it:

| Theme | Wordmark | Body | Licence |
|---|---|---|---|
| Metal | Grenze Gotisch | Grenze (400/700) | SIL OFL 1.1 |
| Punk  | Rubik Dirt     | Special Elite    | OFL 1.1 / **Apache 2.0** |

Grenze is the text companion to Grenze Gotisch from the same type project, so
Metal's body echoes its wordmark by descent rather than by resemblance. Punk
pairs the distressed Rubik Dirt with Special Elite, a battered typewriter.

Special Elite is **Apache 2.0, not OFL** — it lives in google/fonts' `apache/`
tree and ships here as `SpecialElite-LICENSE.txt`. The others ship an
`OFL.txt`. A test asserts every shipped font has a licence file whose contents
name a real licence, because a failed download leaves an HTML error page with
the right filename.

Files are subset to the characters actually rendered. Rubik Dirt is capitals
only (146KB, down from 302KB) because the punk title bar is uppercase, so its
lowercase glyphs could never appear. Special Elite has a single weight, so
bold there is the browser's synthetic bold — acceptable on a face that is
already irregular. There is deliberately no font CDN: it would add an
origin to the Content-Security-Policy and tell a third party about every page
load, and `font-src 'self'` in `app/main.py` enforces that.
