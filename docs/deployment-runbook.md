# Deployment & Operations Runbook

## 1. Hosting Architecture Options & Cost Boundaries

This runbook documents the deployment, firewall, offline model-cache preparation, operational maintenance, and recovery procedures for hosting the platform at a target operating cost of **₹0/month**.

> [!WARNING]
> **Deployment Architecture Reconciled & Approval Boundaries:**
> Two primary hosting architectures exist in the project documentation and evaluation records:
> - **Option A (OCI Always Free VM):** A single Ampere A1 ARM64 instance running Ubuntu 24.04, Docker Compose, Caddy reverse proxy, and local file/database storage.
> - **Option B (Render Web Service + Neon PostgreSQL + Upstash Redis):** A serverless containerized deployment on Render with external serverless database and cache.
>
> **The selection of the final production hosting provider is an item requiring explicit user approval.**
> This runbook provides the operational specifications for both options without unilaterally declaring either one the sole production standard.

---

## 2. Option A: OCI Always Free Deployment (Single-VM Model)

### 2.1. OCI Always Free Quotas & Cost Warnings
To guarantee ₹0/month, the tenancy must remain strictly within OCI Always Free limits:
- **Compute:** Maximum 4 OCPUs and 24 GB RAM (Ampere A1 ARM64).
- **Storage:** Maximum 200 GB total block volume allocation across all boot and block disks.
- **Networking:** Maximum 10 TB/month outbound data transfer.
- **IP Address:** 1 Ephemeral or Reserved IPv4 address.

### 2.2. VM Configuration & Network Security
- **Operating System:** Ubuntu 24.04 LTS (ARM64 / `aarch64`).
- **VM Shape:** `VM.Standard.A1.Flex` (2 OCPUs, 12 GB RAM, 100 GB Boot Volume).
- **OCI Security List Ingress Rules:**
  - Port 22 (SSH): Restricted to administrator IP CIDR.
  - Port 80 (HTTP): Open to `0.0.0.0/0` (Let's Encrypt ACME HTTP-01 challenge & HTTPS redirect).
  - Port 443 (HTTPS): Open to `0.0.0.0/0` (Public web edge).
- **Private Database Enforcement:** Port 5432 is **never** exposed on the public internet; it communicates strictly across the internal Docker bridge network (`sebi-internal-net`).

### 2.3. Offline Model Cache Preparation
The production backend runs with `TRANSFORMERS_OFFLINE=1` and `HF_HUB_OFFLINE=1`. The embedding model (`BAAI/bge-small-en-v1.5`) is pre-staged via `scripts/prepare_model_cache.py`:
```bash
# 1. Download model weights on connected machine:
python scripts/prepare_model_cache.py download --target-dir ./hf_model_cache

# 2. Transfer to VM and set container permissions (unprivileged UID 10001):
sudo mkdir -p /var/lib/docker/volumes/sebi_rag_hf_model_cache/_data
sudo cp -r ./hf_model_cache/* /var/lib/docker/volumes/sebi_rag_hf_model_cache/_data/
sudo chown -R 10001:10001 /var/lib/docker/volumes/sebi_rag_hf_model_cache/_data

# 3. Validate offline load:
python scripts/prepare_model_cache.py validate --cache-dir ./hf_model_cache
```

### 2.4. Service Startup via Docker Compose
```bash
cd /opt/sebi-rag
# 1. Launch database and run migrations
docker compose -f docker-compose.prod.yml up -d postgres alembic-upgrade

# 2. Seed baseline corpus into database (if DB storage enabled)
docker compose -f docker-compose.prod.yml run --rm api \
  python -m backend.app.persistence.importer --db-url "$DATABASE_URL" --processed-dir /app/data/processed

# 3. Launch application services
docker compose -f docker-compose.prod.yml up -d api worker frontend reverse-proxy
```

---

## 3. Option B: Render + Neon + Upstash (Serverless Container Model)

### 3.1. Architectural Separation & Free-Tier Limits
- **Backend Web Service (Render Free Tier):**
  - Limits: 512 MB RAM, 0.1 CPU, spun down after 15 minutes of inactivity (cold start: $\sim 30\text{–}50\text{s}$).
  - Memory Constraint: In-memory embeddings and BM25 index must fit within 512 MB. For multiple collections, lazy index loading is required.
- **Metadata Database (Neon Serverless PostgreSQL - Phase 3 Conditional):**
  - Limits: 0.5 GB storage, 100 compute hours/month on Free tier.
  - Role: Relational document registry, relationship graph edges, query audit logs. Chunks and dense vectors remain in local snapshot files to conserve Neon storage and compute hours.
- **Distributed Cache (Upstash Redis - Deferred / Optional):**
  - Limits: 10,000 commands/day on Free tier.
  - Role: Distributed rate limiting if multiple Render backend workers are deployed.

---

## 4. Environment Configuration & Secret Management

Create `.env.prod` with strict file permissions (`chmod 600`):
```env
APP_ENV=production
# Application Secrets
ADMIN_API_KEY=<GENERATE_SECURE_ADMIN_KEY_MIN_16_CHARS>
FRONTEND_ORIGIN=https://sebi-rag.yourdomain.com
DOMAIN=sebi-rag.yourdomain.com
ACME_EMAIL=admin@yourdomain.com

# Persistence & Storage
PROCESSED_DIR=/app/data/processed
INDEXES_DIR=/app/data/indexes
RAW_DIR=/app/data/raw
BACKUPS_DIR=/app/data/backups

# Rate Limiting
RATE_LIMIT_QUERY_PER_MINUTE=20
RATE_LIMIT_QUERY_BURST=10

# Synthesis Configuration (mock, groq, gemini)
SYNTHESIS_PROVIDER=mock
SYNTHESIS_TIMEOUT_SECONDS=5.0
MAX_SYNTHESIS_EVIDENCE_CHUNKS=5
# GROQ_API_KEY=<OPTIONAL_KEY>
# GEMINI_API_KEY=<OPTIONAL_KEY>
```

---

## 5. Multi-Collection Scale & Memory Considerations

As the platform generalizes beyond the 5-document pilot, memory and index size require careful capacity planning:
1. **Pilot Memory Baseline:** The 5-document SEBI CSCRF corpus (168 chunks) requires $< 30\text{ MB}$ RAM for BM25 tables and normalized dense vectors.
2. **50-Document Projection:** 50 typical regulatory circulars ($\sim 2,000\text{ chunks}$) require $\sim 150\text{ MB}$ RAM, well within Render's 512 MB limit and OCI's 12 GB RAM.
3. **Index Hot-Reload Safety:** When publishing a new index snapshot via `/api/v1/admin/indexes/publish`, the application loads the new snapshot into memory alongside the old snapshot during reload. Peak memory momentarily doubles ($\sim 60\text{ MB} \to 120\text{ MB}$).
4. **Collection-Scoped Lazy Loading:** For dozens of collections, Phase 2 will implement on-demand index loading with an LRU cache to prevent exceeding memory ceilings on free-tier containers.

---

## 6. Backup, Recovery & Rollback Procedures

### 6.1. Coordinated Snapshot Backups
```bash
docker compose -f docker-compose.prod.yml exec api \
  python -m app.backup.cli create \
  --backup-id "prod_$(date +%Y%m%d_%H%M%S)" \
  --include-database
```

### 6.2. Recovery Limitations
- **Local Block Volume Risk:** Backups stored in `/app/data/backups` survive container restarts, but **do not survive VM termination or hypervisor failure**.
- **Off-Host Archival:** Completed backup archives (`*.tar.gz`) should be transferred off-host using `scp` or automated cron jobs.

### 6.3. Rollback
In the event of an unrecoverable failure during deployment:
```bash
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d
```
Active search snapshots can be rolled back instantly by editing `data/indexes/active_snapshot.json` to point to the prior verified snapshot timestamp.
