# Citenza — Deployment Runbook

> **Your documents. Verified answers.**

## 1. Production Architecture Topology

Citenza runs entirely within a $0/month production architecture:
- **Frontend SPA:** Vercel Production
- **Backend API:** Render Free Web Service
- **Database:** Neon Serverless PostgreSQL (ap-southeast-1 direct endpoint)
- **Object Storage:** Backblaze B2 (S3-compatible)
- **Ingestion Worker:** GitHub Actions scheduled runner / Render cron
- **LLM Synthesis:** Groq Free Cloud API

---

## 2. Environment Configuration

### Frontend (Vercel)
- `VITE_API_BASE_URL`: URL of the Render backend web service.
- `VITE_FIREBASE_API_KEY`: Firebase API Key.
- `VITE_FIREBASE_AUTH_DOMAIN`: Firebase Auth Domain.
- `VITE_FIREBASE_PROJECT_ID`: Firebase Project ID.
- `VITE_FIREBASE_STORAGE_BUCKET`: Firebase Storage Bucket.
- `VITE_FIREBASE_MESSAGING_SENDER_ID`: Firebase Messaging Sender ID.
- `VITE_FIREBASE_APP_ID`: Firebase App ID.

### Backend (Render)
- `APP_ENV`: `production`
- `DATABASE_URL`: Neon PostgreSQL pooled connection string (Port 6543).
- `ADMIN_DATABASE_URL`: Neon PostgreSQL direct connection string (Port 5432, for migrations only).
- `ADMIN_API_KEY`: Strong random secret for admin API routes.
- `AUTH_SECRET_KEY`: Strong random secret for local session signing.
- `STORAGE_MODE`: `s3`
- `S3_ENDPOINT_URL`: Backblaze B2 S3 API endpoint.
- `S3_BUCKET_NAME`: Backblaze B2 bucket name.
- `AWS_ACCESS_KEY_ID`: Backblaze B2 application key ID.
- `AWS_SECRET_ACCESS_KEY`: Backblaze B2 application key.
- `GROQ_API_KEY`: Groq API Key.
- `FIREBASE_PROJECT_ID`: Firebase project ID for server-side token validation.
- `FIREBASE_SERVICE_ACCOUNT_JSON`: Firebase service account credentials JSON.

---

## 3. Database Migration Procedure

All migrations are forward-only:
```bash
# Verify current migration head
alembic current

# Run migration 0012 to generalize document models
alembic upgrade head
```

Never renumber or modify applied revisions `0001` through `0011`.

---

## 4. Verification & Health Checks

After deployment, verify the following endpoints:
1. `GET /health` -> HTTP 200 `{"status": "ok"}`
2. `GET /ready` -> HTTP 200 `{"status": "ready", "index_active": true}`
3. `GET /api/v1/collections` -> HTTP 200 with list of active collections.
4. `POST /api/v1/query` with test question -> HTTP 200 with grounded answer and verified citations.
