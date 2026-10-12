# Data Pipeline & Ingestion Architecture

## 1. Scope & Ingestion Lifecycle

The **SEBI Regulatory Intelligence Platform** uses a deterministic, multi-stage ingestion pipeline to transform unstructured official regulatory PDFs into structured, provenance-annotated, and searchable records.

### Ingestion Lifecycle Stages
```text
  [Official PDF]
        |
        v
  1. Registration & Checksum Deduplication (SHA-256)
        |
        v
  2. Document Parsing & Layout Analysis (PyMuPDF / pdfplumber)
        |
        v
  3. Provenance-Preserving Chunking & Boundary Tagging
        |
        v
  4. Vector Embedding & Lexical Inversion (pgvector / BM25)
        |
        v
  5. Relationship Extraction & Evidence Audit (Newer -> AMENDS/RESCINDS -> Older)
        |
        v
  [Published to Search Indexes & Relational Persistence Tier]
```

The existing 5-document Cybersecurity and Cyber Resilience Framework (CSCRF) corpus (`sebi_cscrf_2024_113`, `2025_045`, `2025_060`, `2025_096`, `2025_119`), comprising 168 chunks and 9 verified graph edges, represents the validated **initial pilot and permanent regression suite**. The architecture is structured for dynamic registration of additional circulars across SEBI domains, with future extensibility to secondary official regulators.

---

## 2. Ingestion Connectors & Source Integrity

### Official Regulatory Sources
- **SEBI:** Official SEBI circulars repository (`https://www.sebi.gov.in/legal/circulars/...`).
- **Future Regulators (Phase 8G):** Reserve Bank of India (`rbi.org.in`), IRDAI (`irdai.gov.in`).

### Source Integrity Rules
1. **Immutable Cryptographic Hash:** Every downloaded PDF is hashed with SHA-256 upon arrival. Duplicate hashes trigger immediate deduplication.
2. **Dual-Page Provenance:** Every chunk and evidence quote tracks both the physical 1-indexed PDF page (`pdf_page: 3`) and the document's printed page string (`printed_page: "Page 3 of 8"`).
3. **No Inference of Missing Attributes:** If an official document omits an explicit effective date, the field remains `null` or `unknown`. An issue date is never silently substituted as the effective date.
4. **Extraction Quality Auditing:** Pages with sparse text layers, unreadable fonts, or excessive artifacts are flagged (`extraction_status: "flagged_ocr"` or `"low_density"`).

---

## 3. Relational Database Schema Design (Phase 8A Implemented)

In Phase 8A, flat JSON and NumPy file stores are complemented by a production-ready relational PostgreSQL schema with `pgvector` managed via Alembic (`0001_initial_persistence_foundation`). The persistence model cleanly separates logical documents from immutable physical versions:

### Implemented Tables (`backend/app/persistence/models.py`)

1. **`regulators`**: Master registry of regulatory authorities (`regulator_code` PK, e.g. `'SEBI'`, `'RBI'`, `name`, `jurisdiction`).
2. **`regulatory_domains`**: Regulatory frameworks/domains (`domain_code` PK, e.g. `'CSCRF'`, `'LODR'`, `regulator_code` FK).
3. **`regulatory_documents`**: Logical regulatory documents (`document_id` PK, e.g. `'sebi_cscrf_2024_113'`, `regulator_code` FK, `domain_code` FK, `circular_number`, `title`, `issue_date`, `effective_date`, `source_url`, `status`).
4. **`document_versions`**: Immutable document extractions/revisions (`version_id` PK, `document_id` FK, `version_number`, `sha256` unique per version, `total_pages`, `extracted_at`, `is_active`). Re-ingestion with a changed PDF checksum creates an immutable new version without overwriting historical records.
5. **`provisions`**: Clause-level anchors (`provision_id` PK, `document_id` FK, `section_code`, `title`, `pdf_page`, `printed_page`).
6. **`chunks`**: Text slices with dual-page provenance (`chunk_id` PK, `version_id` FK, `chunk_index`, `text`, `pdf_page_start`, `pdf_page_end`, `printed_page_start`, `printed_page_end`, `is_ocr`).
7. **`chunk_embeddings`**: Dense vectors (`chunk_id` PK/FK, `embedding` vector(384), `model_name`, `dimension`). Exact cosine distance queries utilize pgvector's `<=>` operator.
8. **`regulatory_edges`**: Directed legal relationships (`edge_id` PK, `source_document_id` FK, `target_document_id` FK, `relationship_type` [`AMENDS`, `RESCINDS`, `REFERENCES`], `scope` [`full`, `partial`], `affected_provisions` JSONB, `evidence_text`, `evidence_pdf_page`, `evidence_printed_page`, `review_status` [`verified`, `needs_review`, `rejected`], `confidence`). Check constraint enforces `source_document_id != target_document_id`.
9. **`ingestion_jobs`**: Asynchronous batch and job tracking (`job_id` PK, `document_id` FK, `status`, `error_message`, `started_at`, `completed_at`).

### Idempotent Corpus Importer CLI (`backend/app/persistence/importer.py`)

The pilot CSCRF corpus is transferred to PostgreSQL via a transactional, idempotent importer:
- **Command:** `python backend/app/persistence/cli_importer.py --database-url <URL> --all-or-nothing`
- **Dry-Run Mode:** `python backend/app/persistence/cli_importer.py --dry-run` performs full offline validation of manifest JSONs, chunk boundaries, embedding shapes, and graph cycles without modifying the database.
- **Edge Protection:** Previously verified edges (`review_status = 'verified'`) are strictly preserved during subsequent imports.
- **Transactional Rollback:** The `--all-or-nothing` flag ensures atomic rollbacks on any schema or data integrity error.

---

## 4. Chunking Strategy & Determinism

1. **Target Chunk Size:** Bounded chunks (approximately 350 tokens / 1,500 characters) preserving paragraph and section integrity.
2. **Boundary Sensitivity:** Chunks prefer natural paragraph and provision boundaries over rigid character offsets.
3. **Table Isolation:** Tabular data (such as compliance glide path schedules) is preserved in distinct table chunks to prevent structural flattening.
4. **Header Enrichment:** Chunks are prefixed with their document title and section headers to ensure dense vector representation captures semantic context.

---

## 5. Regulatory Relationship Direction & Status Resolution

### Direction Standard
All graph edges are strictly directed as:
$$\text{source\_document\_id (acting / newer)} \xrightarrow{\text{RELATIONSHIP}} \text{target\_document\_id (affected / older)}$$

### Supported Edge Types
- `AMENDS`: Modifies specific clauses or terms of an earlier document. Triggers supersession logic (`is_supersession_inducing = True`).
- `RESCINDS`: Repeals an earlier circular in whole or in part. Triggers supersession logic (`is_supersession_inducing = True`).
- `REFERENCES`: Cites an earlier circular for statutory context, definitions, or preambles. Does **not** induce supersession (`is_supersession_inducing = False`).

### Provision-Level Scoping Rule
When `scope = 'partial'`, only the specific clauses listed in `affected_provisions` (e.g. `["Clause 6.11"]`) are marked as amended or superseded. The remainder of the target circular continues in full legal effect.
