# Citenza — Architecture Decision Records (ADRs)

> **Your documents. Verified answers.**

## ADR Index
- **ADR-001:** Forward-Only Database Migrations (No Revision Rewriting)
- **ADR-002:** Dual-Engine Persistence Abstraction (File and PostgreSQL/pgvector)
- **ADR-003:** Multi-Tenant Workspace & Collection Isolation Model
- **ADR-004:** Mechanical Citation Verification & Deterministic Abstention
- **ADR-005:** General-Purpose Platform Rebranding to Citenza
- **ADR-006:** Complete Legacy SEBI/CSCRF Removal & Generalization (Migration 0012)

---

### ADR-001: Forward-Only Database Migrations
- **Status:** Accepted
- **Context:** Neon PostgreSQL database has migrations `0001` through `0011` applied in production. Rewriting or renumbering past migrations risks database drift and corrupts production state.
- **Decision:** All future schema adjustments must be introduced as forward-only Alembic migrations (e.g. `0012_generalize_document_model.py`). Revisions `0001`–`0011` remain immutable historical records.

---

### ADR-002: Dual-Engine Persistence Abstraction
- **Status:** Accepted
- **Context:** The platform must run in lightweight developer environments, offline demo mode, and fully hosted PostgreSQL environments without code bifurcation.
- **Decision:** Maintain a `StorageRepositoryFactory` interface with both `FileStorageRepositoryFactory` (local JSON files/directories) and `PostgresStorageRepositoryFactory` (SQLAlchemy 2.0 / PostgreSQL).

---

### ADR-003: Multi-Tenant Workspace & Collection Isolation
- **Status:** Accepted
- **Context:** The platform serves multiple independent users and teams while maintaining public demo collections.
- **Decision:** Partition all tenant-created data by `workspace_id`. Require workspace authorization on all API routes. Public demo collections (`general_synthetic`, `cisa_playbooks`, `demo_finops`) are read-only system collections with system workspace identifiers.

---

### ADR-004: Mechanical Citation Verification & Deterministic Abstention
- **Status:** Accepted
- **Context:** LLM hallucinations cannot be tolerated in high-consequence enterprise or technical research.
- **Decision:** Every synthesized factual claim must be mechanically verified against server-side retrieved chunks. Claims must match source text verbatim. If evidence is insufficient, contradictory, or absent, the system must deterministically emit `CONTROLLED_ABSTENTION`.

---

### ADR-005: Platform Rebranding to Citenza
- **Status:** Accepted
- **Context:** The product vision is a general-purpose AI document intelligence platform for any documents, not just regulatory compliance.
- **Decision:** Rebrand the application to **Citenza** with tagline: *"Your documents. Verified answers."* Modernize the landing page, UI headers, badges, and documentation to reflect general-purpose document intelligence.

---

### ADR-006: Complete Legacy SEBI/CSCRF Removal & Generalization
- **Status:** Accepted
- **Context:** The codebase contained legacy SEBI-specific datasets, circular PDFs, and test fixtures that prevented true domain neutrality.
- **Decision:**
  - Delete all legacy CSCRF PDFs, circular chunk folders, and SEBI test artifacts.
  - Apply forward-only migration `0012_generalize_document_model.py` making `domain_id` nullable in database models.
  - Pave the way for any arbitrary PDF collection under `GroundingPolicyType.GENERAL_STATIC`.
