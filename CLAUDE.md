# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Development uses the `venv/` virtualenv at the repo root. Activate it (or prefix commands with `venv/bin/`) before running Django.

- Run server: `python manage.py runserver`
- Apply migrations: `python manage.py migrate`
- Make migrations: `python manage.py makemigrations`
- Create superuser: `python manage.py createsuperuser`
- Run all tests: `python manage.py test`
- Run a single test: `python manage.py test core.tests.TestClass.test_method`
- Collect static: `python manage.py collectstatic --noinput`
- Install Playwright browser (required for PDF rendering): `python -m playwright install chromium`

Custom management commands:
- `python manage.py send_whatsapp_followups` — dispatches due WhatsApp follow-ups via Twilio (intended for cron).
- `python manage.py regenerate_pdfs [--dry-run]` — regenerates stored PDFs for all invoices.

Docker: `docker build -t invoicegen .` then run the image; `docker-entrypoint.sh` installs Chromium, runs `collectstatic` + `migrate`, and starts gunicorn on port 8000.

## Architecture

Single-app Django 5.2 project. The project package is `invoicegen/` (settings, URLs, WSGI/ASGI) and all business logic lives in the `core/` app.

**Configuration** (`invoicegen/settings.py`): Uses `django-environ`; reads `.env` at the repo root. Database selection is layered: `DATABASE_URL` → `POSTGRES_*` vars → SQLite fallback (`db.sqlite3`). WhiteNoise serves compressed static files. `STATICFILES_DIRS` is built dynamically from candidate paths that actually exist (including `resources/`), because the logo and signature PNGs are resolved via `staticfiles.finders`. Third-party integrations are configured via `GOOGLE_CLIENT_*` and `TWILIO_*` env vars; absence of these disables the relevant feature paths rather than erroring at import.

**Domain model** (`core/models.py`):
- `Client` has many `Invoice`s; each `Invoice` has many `InvoiceItem`s. `Invoice.invoice_type` is either `GENERAL` or `PROFORMA` and selects which template/fields are used — proforma invoices use a separate set of vehicle/price fields (`proforma_*`) and a different PDF template. GCT is a hardcoded 15% applied only to parts subtotal.
- `Invoice` owns PDF generation end-to-end: `_render_pdf()` uses Playwright (headless Chromium) to render `invoices/detail_pdf.html` or `detail_pdf_proforma.html` to A4 PDF. Logo and signature are embedded as base64 data URLs so PDFs are self-contained. `generate_pdf_bytes()` dispatches to the right variant based on `invoice_type`. PDFs may be stored locally on `pdf_file` and/or uploaded to Google Drive; the `drive_*` fields track Drive state, and `mark_drive_file()` is the canonical way to record a successful upload (it also clears the local copy).
- `GoogleAccount` is per-user OAuth state. `get_credentials()` refreshes expired tokens and persists them. This is the only place that should touch stored Google credential JSON.
- WhatsApp follow-ups are a three-model system: `WhatsAppSettings` is a singleton (load via `.load()`), `WhatsAppFollowUp` tracks schedule per-client with override support, `WhatsAppMessageLog` is an append-only audit log. `register_success()` advances `next_follow_up_date` by the interval from the send date (keeps follow-ups recurring).

**Integration boundaries**:
- `core/google.py` — all Google OAuth, Drive upload/download, and Gmail send logic. Views never call google APIs directly.
- `core/whatsapp.py` — all Twilio interactions; raises `WhatsAppConfigurationError` / `WhatsAppSendError` that views catch.
- Both modules are import-safe without credentials; errors surface only when functions are actually called.

**Views and URLs** (`core/views.py`, `core/urls.py`): Function-based views, all behind `@login_required` except signup. Routes are grouped by concern: clients, invoices (nested under a client for creation), Google OAuth connect/callback/disconnect + Drive folder picker, WhatsApp manager + per-followup update/send-now + Twilio status webhook.

**Templates** (`templates/`): Project-level templates dir (`DIRS` set in settings). `invoices/detail_pdf*.html` are standalone documents rendered by Playwright — they receive `logo_src` and `signature_src` as data URLs and must be self-contained (no external fetches). The interactive `invoices/detail.html` shares partials (`_detail_document*.html`) with the PDF templates.

**Static/media**: `resources/` contains the logo and signature images and is registered as a staticfiles dir. `media/invoices/` stores locally-generated PDFs when not offloaded to Drive.
