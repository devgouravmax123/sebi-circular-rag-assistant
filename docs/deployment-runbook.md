# OCI Always Free Deployment and Operational Runbook

This runbook documents the deployment, firewall, offline model-cache preparation, operational maintenance, and recovery procedures for hosting the **SEBI Regulatory Q&A Assistant** on Oracle Cloud Infrastructure (OCI) Always Free tier at an operating target of **₹0/month**.

---

## 1. Preconditions and Cost Warnings

> [!WARNING]
> **Zero-Cost Billing Disclaimer:**
> OCI budget alerts are notification tools only; they are **NOT** hard spending cutoffs. If an account is converted to Pay-As-You-Go and provisions resources beyond Always Free quotas, billing occurs immediately.
>
> To guarantee ₹0/month, the tenancy must remain strictly within the OCI Always Free resource limits:
> - **Compute:** Maximum 4 OCPUs and 24 GB RAM (Ampere A1 ARM64).
> - **Storage:** Maximum 200 GB total block volume allocation across all boot and block disks.
> - **Networking:** Maximum 10 TB/month outbound data transfer.
> - **IP Address:** 1 Ephemeral or Reserved IPv4 address.

### Account Verification Hold
During registration, Oracle places a temporary pre-authorization hold (typically ₹80–₹100 or $1 USD) on the credit card to verify identity. This is automatically reversed within 3–5 business days.

---

## 2. Infrastructure Sizing & OCI Capacity Verification

### Target VM Configuration
- **Operating System:** Ubuntu 24.04 LTS (Canonical Minimal or Standard Server for ARM64 / `aarch64`).
- **Shape:** `VM.Standard.A1.Flex`
- **OCPUs:** 2 (can scale up to 4).
- **Memory:** 12 GB RAM (conservative baseline; provides ample headroom for Python runtimes without memory pressure).
- **Boot Volume:** 100 GB (well below the 200 GB combined account limit).

### Home-Region Capacity Check
OCI Ampere A1 capacity is constrained in certain regions (e.g. `ap-mumbai-1`, `ap-hyderabad-1`, `us-ashburn-1`).
- Attempt launching the instance in your designated home region.
- If an "Out of host capacity" error is encountered, retry periodically or select an alternate availability domain within the same region.

---

## 3. Network Architecture and Firewall Configuration

### OCI Virtual Cloud Network (VCN) Ingress Security List
Configure the subnet's Security List with the following **Inbound Rules**:

| Source CIDR | IP Protocol | Destination Port | Purpose | Security Guidance |
| :--- | :---: | :---: | :--- | :--- |
| `<YOUR_ADMIN_IP>/32` | TCP | `22` | SSH Administration | **Strictly restrict to admin IP where practical**; avoid `0.0.0.0/0` unless key-only SSH with fail2ban is enforced. |
| `0.0.0.0/0` | TCP | `80` | HTTP Edge Traffic | Required for Let's Encrypt ACME HTTP-01 challenge and HTTP->HTTPS redirect. |
| `0.0.0.0/0` | TCP | `443` | HTTPS Public Edge | Public reverse proxy serving React frontend and `/api/v1/query`. |

### Outbound Connectivity
- **Egress Rule:** Allow All Outbound Traffic (`0.0.0.0/0`) for package updates, Let's Encrypt TLS renewal, SEBI circular PDF downloads, and optional external LLM API calls.

### Private Database Enforcement
- **PostgreSQL Port 5432 is NEVER exposed on the host or public internet.**
- It communicates exclusively within the private Docker bridge network (`sebi-internal-net`).

### Host Firewall (UFW on Ubuntu)
Run on the VM:
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

---

## 4. DNS and Public HTTPS Prerequisites

Caddy automatically provisions Let's Encrypt / ZeroSSL TLS certificates. This requires:
1. A valid public domain or subdomain (e.g., free DuckDNS, or a domain managed via Cloudflare).
2. DNS A record pointing the domain to the VM's Public IPv4 address.
3. Inbound TCP ports 80 and 443 accessible from the public internet.

---

## 5. Environment Configuration & Secret Permissions

Create `/opt/sebi-rag/.env.prod` with restrictive file permissions:
```bash
sudo mkdir -p /opt/sebi-rag/data/backups /opt/sebi-rag/data/indexes /opt/sebi-rag/data/processed /opt/sebi-rag/data/raw
sudo chown -R 10001:10001 /opt/sebi-rag/data
sudo chmod 600 /opt/sebi-rag/.env.prod
```

### Template `.env.prod`
```env
APP_ENV=production
POSTGRES_USER=sebi_app
POSTGRES_PASSWORD=<GENERATE_SECURE_PASSWORD_32_CHARS>
POSTGRES_DB=sebi_regulatory_db
DATABASE_URL=postgresql+asyncpg://sebi_app:<GENERATE_SECURE_PASSWORD_32_CHARS>@postgres:5432/sebi_regulatory_db
ADMIN_API_KEY=<GENERATE_SECURE_ADMIN_KEY_MIN_16_CHARS>
DOMAIN=sebi-rag.yourdomain.com
ACME_EMAIL=admin@yourdomain.com
FRONTEND_ORIGIN=https://sebi-rag.yourdomain.com
SYNTHESIS_PROVIDER=mock
WEB_CONCURRENCY=1
RATE_LIMIT_QUERY_PER_MINUTE=20
RATE_LIMIT_QUERY_BURST=10
```

---

## 6. Offline Model Cache Preparation

The production backend runs with `TRANSFORMERS_OFFLINE=1` and `HF_HUB_OFFLINE=1`. The embedding model (`BAAI/bge-small-en-v1.5`) must be staged prior to container startup.

### Step 1: Pre-download model weights (on machine with internet access)
Run the preparation script to download pinned revision `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`:
```bash
python scripts/prepare_model_cache.py download --target-dir ./hf_model_cache
```
*Note on Directory Structure:* The download tool creates `./hf_model_cache/hub/models--BAAI--bge-small-en-v1.5/...`. The root directory `./hf_model_cache/` directly contains `hub/`.

### Step 2: Transfer to VM and set container permissions
The container environment sets `HF_HOME=/home/sebi/.cache/huggingface`, which means Hugging Face looks for `$HF_HOME/hub`. Therefore, the entire `./hf_model_cache/` folder contents (including the `hub/` directory) must be placed at the root of the volume:
```bash
# On the OCI VM:
sudo mkdir -p /var/lib/docker/volumes/sebi_rag_hf_model_cache/_data
# Copy the entire contents (including hub/) into the volume data root:
sudo cp -r ./hf_model_cache/* /var/lib/docker/volumes/sebi_rag_hf_model_cache/_data/
# Set ownership to unprivileged application user UID 10001 (sebi):
sudo chown -R 10001:10001 /var/lib/docker/volumes/sebi_rag_hf_model_cache/_data
```
*Verification Check:* The file `/var/lib/docker/volumes/sebi_rag_hf_model_cache/_data/hub/models--BAAI--bge-small-en-v1.5/snapshots/5c38ec7c405ec4b44b94cc5a9bb96e735b38267a/model.safetensors` must exist.

### Step 3: Validate offline load on VM
Pass the parent cache root directory (the directory containing `hub/`) to `--cache-dir`:
```bash
python scripts/prepare_model_cache.py validate --cache-dir ./hf_model_cache
```
This sets `HF_HOME=./hf_model_cache`, enforces `TRANSFORMERS_OFFLINE=1` and `HF_HUB_OFFLINE=1`, and loads the SentenceTransformer locally to confirm the cache is complete before starting containers.


---

## 7. Service Startup and Seeding Sequence

### Step 1: Launch Database & Run Migrations
```bash
cd /opt/sebi-rag
docker compose -f docker-compose.prod.yml up -d postgres alembic-upgrade
```
Verify migration status:
```bash
docker compose -f docker-compose.prod.yml logs alembic-upgrade
```

### Step 2: Seed Baseline Regulatory Corpus
Import the authoritative baseline circular corpus into the database:
```bash
docker compose -f docker-compose.prod.yml run --rm api \
  python -m backend.app.persistence.importer \
  --db-url "$DATABASE_URL" \
  --processed-dir /app/data/processed
```

### Step 3: Launch Web, API, Worker, and Proxy
```bash
docker compose -f docker-compose.prod.yml up -d api worker frontend reverse-proxy
```

### Step 4: Validate Service Health
```bash
# Internal readiness
docker compose -f docker-compose.prod.yml exec api curl -f http://127.0.0.1:8000/ready

# Public edge health
curl -f https://sebi-rag.yourdomain.com/health
curl -f https://sebi-rag.yourdomain.com/ready
```

---

## 8. Backup Verification & Recovery Limitations

### Creating a Production Coordinated Backup
```bash
docker compose -f docker-compose.prod.yml exec api \
  python -m app.backup.cli create \
  --backup-id "prod_$(date +%Y%m%d_%H%M%S)" \
  --include-database
```

### Important Recovery Limitations
- **Local Storage Risk:** Backups created inside `/app/data/backups` reside on the VM's local block volume. They survive container restarts and upgrades, but **DO NOT survive catastrophic VM termination, hypervisor failure, or storage corruption**.
- **Recommended Off-Host Procedure:** Periodically transfer completed `*.tar.gz` archives off the VM to secure offline storage using `scp` or an automated rsync/cron job.

---

## 9. Rollback & Troubleshooting

### Viewing Logs
```bash
docker compose -f docker-compose.prod.yml logs --tail=100 -f api
docker compose -f docker-compose.prod.yml logs --tail=100 -f reverse-proxy
```

### Emergency Rollback
To stop services cleanly and retain all database and index volumes:
```bash
docker compose -f docker-compose.prod.yml down
```
To restart cleanly:
```bash
docker compose -f docker-compose.prod.yml up -d
```
