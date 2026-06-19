# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Copy `.env.example` to `.env` and fill in values before running. The app requires `DATABASE_URL` and `JWT_SECRET` (min 32 chars) at minimum.

In production the app runs via `Procfile`: `uvicorn main:app --host 0.0.0.0 --port $PORT` (Railway/Heroku).

There are no automated tests.

## Architecture

Single-file FastAPI app (`main.py`) with async SQLAlchemy + PostgreSQL. All routes, background tasks, middleware, and business logic live in `main.py`. Supporting modules:

| File | Role |
|---|---|
| `models.py` | SQLAlchemy ORM models |
| `schemas.py` | Pydantic request/response schemas |
| `auth.py` | JWT creation/validation, bcrypt password hashing |
| `database.py` | Async engine + session factory |
| `email_service.py` | SMTP email sending (aiosmtplib) |
| `zapsign.py` | ZapSign API client (digital signature) |
| `pdf_generator.py` | ReportLab fallback PDF generation |

### Database migrations

No Alembic. All `ALTER TABLE … ADD COLUMN IF NOT EXISTS` migrations run inline at startup inside the `lifespan` function in `main.py` (around line 178). Add new columns there.

### Two-stage client flow

1. **Etapa 1** — Client fills in registration form (`/cadastro`) → stored in `Cadastro` table, agendamento (appointment) booked, email sent.
2. **Etapa 2** — Admin liberates via `/admin/cadastro/{id}/liberar-etapa2` → client receives link → opens `/etapa2/{token}` → clicks sign → `POST /api/etapa2/{token}/zapsign` is called, which fills the DOCX template and sends to ZapSign at that moment (not before). After signing, documents are uploaded.

**Key implication:** client data can be corrected right up until the client clicks "Assinar" on the Etapa 2 page. Once `zapsign_doc_token` is set on the cadastro, the document has been generated and edits no longer affect it.

### ZapSign integration

Two modes controlled by `ZAPSIGN_TEMPLATE_ID`:
- **Set**: uses `criar_via_modelo()` — fills variables in a DOCX template hosted on ZapSign. Variables map via `_kit_campos()` in `main.py`.
- **Not set**: uses `criar_documento()` — generates a PDF with ReportLab and uploads it.

`ZAPSIGN_SANDBOX=true` by default. Set to `false` for production.

### Document storage

Two modes controlled by `AWS_S3_BUCKET_NAME`:
- **Set**: files go to S3/S3-compatible storage.
- **Not set**: files stored locally under `RAILWAY_VOLUME_MOUNT_PATH` (default `./storage`).

### Auth model

Two roles: `"admin"` (full access, manages users, can delete) and `"juridico"` (manages cases and agenda, cannot manage users or delete). JWT stored in httponly cookie `access_token` (8h expiry).

- `get_current_admin` — validates JWT, returns email string
- `get_current_admin_obj` — returns full `AdminUsuario` ORM object (use when role matters)
- `require_admin_role` — 403 if not admin

### Background tasks

Started in `lifespan`: `cleanup_temp_files()` runs hourly, `processar_lembretes()` runs every 30 min to send lead reminder emails.

### Atendente assignment

Admins assign cases to jurídico users via `PUT /admin/cadastro/{id}/atendente`. Jurídico users can only pull cases to themselves (`atendente_id = current.id`) or release their own. The `atendente` relationship uses `lazy="selectin"` so it loads automatically on any `Cadastro` query.

## Key environment variables

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | Yes | `postgresql+psycopg://…` |
| `JWT_SECRET` | Yes | Min 32 chars |
| `ADMIN_INITIAL_EMAIL` / `ADMIN_INITIAL_PASSWORD` | For seed | Creates first admin on startup |
| `ZAPSIGN_API_TOKEN` | For signing | |
| `ZAPSIGN_TEMPLATE_ID` | Optional | If set, uses DOCX template |
| `ZAPSIGN_SANDBOX` | — | Default `true` |
| `SMTP_HOST/PORT/USER/PASSWORD` | For email | Default: Gmail port 587 |
| `BASE_URL` | For email links | e.g. `https://yourdomain.com` |
| `AWS_S3_BUCKET_NAME` | For S3 storage | Optional |
