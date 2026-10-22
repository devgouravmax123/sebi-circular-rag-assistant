# Data Pipeline & Ingestion Architecture

## 1. Overview & Ingestion Lifecycle

The platform uses a deterministic, multi-stage ingestion pipeline to transform unstructured official regulatory PDFs into structured, provenance-annotated, and searchable text chunks.

```text
  [Official PDF File]
         |
         v
  1. Registration & Path Security Validation (traversal & extension checks)
         |
         v
  2. SHA-256 Checksum Computation & Deduplication
         |
         v
  3. Text Extraction & Quality Auditing (PyMuPDF: OK, SPARSE, EMPTY)
         |
         v
  4. Provenance-Preserving Sliding-Window Chunking (350 words, 40 overlap)
         |
         v
  5. Processed JSON Artifact Persistence (document, pages, chunks, manifest)
         |
         v
  6. Hybrid Index Build (BM25 token matrix & BGE-Small dense vectors)
         |
         v
  7. Atomic Index Snapshot Publication (data/indexes/snapshots/{snapshot_id}/)
```

The 5-document SEBI CSCRF circular corpus (`sebi_cscrf_2024_113`, `2025_045`, `2025_060`, `2025_096`, `2025_119`), comprising 168 chunks and 9 verified graph edges, represents the validated **initial reference collection and permanent regression anchor**.

---

## 2. Ingestion Connectors & Source Integrity Rules (`IMPLEMENTED AND VERIFIED`)

1. **Immutable Cryptographic Hash:** Every PDF is hashed with SHA-256 upon arrival. If an active document with an identical hash exists, duplicate re-ingestion is short-circuited.
2. **Dual-Page Provenance:** Every chunk tracks both its physical 1-indexed PDF page (`page_start: 3`) and the document's printed page identifier (`printed_page_start: "Page 3 of 8"`).
3. **Extraction Quality Auditing:** Every extracted page is classified by text density:
   - `OK`: Extracted character count $\ge 200$.
   - `SPARSE`: $1 \le \text{characters} < 200$ (flagged for review).
   - `EMPTY`: Zero characters extracted (flagged as unreadable or image-only).
4. **No Inference of Missing Attributes:** Missing publication or effective dates remain `null`. An issue date is never silently substituted as an effective date.
5. **Path Traversal Protection:** All file paths are strictly resolved relative to approved project data roots. Attempts to access parent directories (`../`) or absolute paths outside the data root are rejected.

---

## 3. Phase 1: Manifest-Driven Local Ingestion Design

In **Phase 1**, static Python registries (`backend/app/ingestion/corpus_registry.py`) are replaced with declarative JSON/YAML manifests stored in `data/manifests/`. This allows adding new documents or collections without modifying application source code.

### 3.1. Proposed Manifest Schema (`data/manifests/{collection_id}.json`)
```json
{
  "collection_id": "sebi_cscrf",
  "name": "SEBI Cybersecurity and Cyber Resilience Framework",
  "description": "Official master circulars and technical revisions for CSCRF compliance.",
  "regulator": "SEBI",
  "domain": "CSCRF",
  "enforce_regulatory_graph": true,
  "documents": [
    {
      "document_id": "sebi_cscrf_2024_113",
      "title": "Cybersecurity and Cyber Resilience Framework (CSCRF) for SEBI Regulated Entities",
      "pdf_path": "data/raw/CSCRF_2024_113.pdf",
      "circular_number": "SEBI/HO/ITD/ITD-PoD-1/P/CIR/2024/113",
      "issue_date": "2024-08-20",
      "effective_date": "2025-01-01",
      "document_type": "master_framework",
      "source_url": "https://www.sebi.gov.in/legal/circulars/aug-2024/cybersecurity-and-cyber-resilience-framework-cscrf-for-sebi-regulated-entities-res-_85973.html"
    },
    {
      "document_id": "sebi_cscrf_2025_045",
      "title": "Extension of compliance timelines for CSCRF",
      "pdf_path": "data/raw/CSCRF_2025_045.pdf",
      "circular_number": "SEBI/HO/ITD-1/ITD_CSC_EXT/P/CIR/2025/45",
      "issue_date": "2025-03-28",
      "effective_date": "2025-03-28",
      "document_type": "extension_circular",
      "source_url": "https://www.sebi.gov.in/legal/circulars/mar-2025/extension-of-compliance-timelines-for-cscrf_93155.html"
    }
  ]
}
```

### 3.2. Manifest Validation Contract
1. **Schema Validation:** Mandatory fields (`collection_id`, `document_id`, `title`, `pdf_path`) must be non-empty strings.
2. **Path Containment:** `pdf_path` must resolve strictly within the project's authorized raw data root (`data/raw/`). Path traversal (`../`) is blocked.
3. **Duplicate Prevention:** Document IDs within a manifest (and across active manifests) must be globally unique.
4. **Graceful Failure:** If a single PDF in a manifest is unreadable or malformed, the ingestion run logs the error to an execution report, leaves existing indexed documents untouched, and fails safely.
5. **Zero Database Requirement:** Manifest-driven ingestion writes processed JSON files to `data/processed/{doc_id}/` and triggers index publication to generate snapshot files locally without requiring a database connection.

### 3.3. CLI Ingestion Execution & Path Resolution
The ingestion CLI module resides at `backend/app/ingestion/cli.py`. Because the application package root is `backend/app/`, invoking `python -m app.ingestion.cli` requires setting the Python module search path.

#### Supported CLI Ingestion Commands
- **Option 1 (From repository root in PowerShell):**
  ```powershell
  $env:PYTHONPATH="backend"; .\.venv\Scripts\python.exe -m app.ingestion.cli ingest-manifest --manifest data/manifests/sebi_cscrf.json
  ```
- **Option 2 (From `backend/` directory in PowerShell):**
  ```powershell
  cd backend
  ..\.venv\Scripts\python.exe -m app.ingestion.cli ingest-manifest --manifest ../data/manifests/sebi_cscrf.json
  ```

#### Path Resolution Distinction
- **Manifest Argument Path (`--manifest <path>`):** Resolved relative to the **current working directory** from which the command is executed.
- **Document PDF Paths (`pdf_path` inside manifest):** Resolved relative to the **repository root** (e.g., `"data/raw/CSCRF_2024_113.pdf"` resolves to `<repo_root>/data/raw/CSCRF_2024_113.pdf`), ensuring manifest definitions remain portable regardless of execution directory.

---

## 4. Chunking Strategy & Determinism (`IMPLEMENTED AND VERIFIED`)

- **Word-Level Sliding Window:** Default chunk size is **350 words** with an overlap of **40 words** (`backend/app/ingestion/chunker.py:TextChunker`).
- **Stable Chunk Identifiers:** Chunk IDs are deterministically generated as:
  $$\text{chunk\_id} = \{\text{document\_id}\}\_\text{p}\{\text{page\_start}\}\_\{\text{chunk\_idx}\}$$
  Example: `sebi_cscrf_2024_113_p66_56`.
- **Dual-Page Annotation:** If a chunk spans across page boundaries (e.g. from page 66 to 67), both `page_start` and `page_end` are recorded alongside the corresponding printed page numbers.
- **Section & Header Retention:** Paragraph breaks and section headings are retained in chunk text to preserve semantic clarity for embedding models.

---

## 5. Regulatory Relationships & Temporal Resolution

### 5.1. Direction Standard
For collections enforcing regulatory relationships, edges are strictly directed:
$$\text{source\_document\_id (acting / newer circular)} \xrightarrow{\text{RELATIONSHIP}} \text{target\_document\_id (affected / older circular)}$$

### 5.2. Edge Types & Supersession Semantics
- `AMENDS`: Modifies specific clauses or requirements of an earlier circular (`is_supersession_inducing = True`).
- `RESCINDS`: Repeals an earlier circular in whole or in part (`is_supersession_inducing = True`).
- `REFERENCES`: Informational background citation or statutory preamble (`is_supersession_inducing = False`). Does not alter legal currency.

### 5.3. Provision Scoping Rule
When an edge specifies `scope = "partial"`, only the specific clauses listed in `affected_provisions` (e.g. `["Section 2.1", "Clause 6.11"]`) are marked as amended or superseded. The remainder of the target document continues in full legal effect.

---

## 6. Persistence Tier: Local Default vs. Relational Option

### Active Production Default: Local Snapshot Files (`IMPLEMENTED AND VERIFIED`)
The active runtime stores and serves indexes entirely from local files:
- `data/processed/{doc_id}/`: Stores `document.json`, `pages.json`, `chunks.json`, `manifest.json`.
- `data/indexes/snapshots/{snapshot_id}/`: Stores `chunks.json`, `bm25_index.pkl`, `dense_index.npy`, and `manifest.json`.
- Memory Footprint: $< 100\text{ MB}$ RAM for pilot corpus; sub-10ms retrieval latency; zero cloud database cost.

### Relational Database Option (`IMPLEMENTED, VERIFICATION INCOMPLETE - Phase 3 Conditional`)
SQLAlchemy models (`backend/app/persistence/models.py`) and Alembic migration `0001_initial_persistence_foundation` define tables for `regulators`, `regulatory_domains`, `regulatory_documents`, `document_versions`, `provisions`, `chunks`, `chunk_embeddings`, `regulatory_edges`, and `ingestion_jobs`.
- **Status:** Validated in isolated persistence tests (`test_persistence_parity.py`), but **not** the active runtime default.
- **Phase 3 Evolution:** Relational persistence is deferred to Phase 3 and made conditional on demonstrated operational need (e.g., concurrent multi-user write requirements or durable query audit logging). Vector storage remains in local snapshot files.
