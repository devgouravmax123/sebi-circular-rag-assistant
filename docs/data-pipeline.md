# Data Pipeline and Supersession Model

## 1. Scope
The initial corpus must be small, curated, and manually checked. Start with a few official SEBI circulars to validate the pipeline before attempting hundreds of documents.

## 2. Source collection
For each source PDF, record:
- stable internal `document_id`
- official circular/reference number, if present
- title
- issuing department, if available
- issue date
- effective date, if explicitly available
- official source URL
- retrieval timestamp
- SHA-256 checksum
- PDF filename
- extraction status and notes

Do not infer missing metadata without marking it as inferred or unknown. Respect source-site terms and avoid aggressive crawling.

## 3. Parsing and provenance
- Use PyMuPDF for page-level text extraction.
- Use pdfplumber when table structure matters.
- Use OCR only for pages whose text layer is missing or inadequate.
- Preserve page numbers and indicate whether a passage came from OCR.
- Record page-level extraction quality and flag suspiciously short/garbled pages.
- Do not silently discard tables, annexures, footnotes, or scanned pages.
- Keep the original PDF unchanged and store its checksum.

## 4. Suggested record schemas

### Document
```json
{
  "document_id": "internal-stable-id",
  "circular_number": null,
  "title": "Title as found in source",
  "department": null,
  "issue_date": null,
  "effective_date": null,
  "source_url": "official-source-url",
  "retrieved_at": "ISO-8601 timestamp",
  "sha256": "hex-checksum",
  "pdf_path": "relative/path.pdf",
  "extraction_status": "pending_review"
}
```

### Chunk
```json
{
  "chunk_id": "stable-chunk-id",
  "document_id": "internal-stable-id",
  "text": "Extracted text",
  "page_start": 1,
  "page_end": 1,
  "is_ocr": false,
  "chunk_type": "body",
  "metadata": {}
}
```

### Graph edge
```json
{
  "edge_id": "stable-edge-id",
  "source_document_id": "newer-acting-circular-id",
  "target_document_id": "older-affected-circular-id",
  "relationship": "AMENDS",
  "scope": "partial",
  "affected_provisions": ["Clause 4.2", "Paragraph 5"],
  "evidence_text": "Exact supporting sentence or short excerpt",
  "evidence_page": 4,
  "confidence": "medium",
  "review_status": "needs_review"
}
```
**Graph Direction Standard:** All graph edges are strictly directed as `Newer/acting circular -> RELATIONSHIP -> Older/affected circular`. For example, if Circular B (2024) amends Circular A (2020), the edge is `B -> AMENDS -> A`.

## 5. Chunking
Initial experiment: roughly 350 words per chunk with 40-word overlap. These are starting parameters, not guaranteed optimal settings. Keep tables as separate chunks when flattening would destroy their meaning. Attach circular metadata, provision/section headers where present, and page range to every chunk.

## 6. Relationship types and partial supersession
- `AMENDS`: a newer circular modifies an earlier circular or specified provisions.
- `RESCINDS`: a newer circular explicitly rescinds/repeals an earlier circular or specified provisions.
- `REFERENCES`: a newer circular refers to another without necessarily changing it.

**Partial supersession and scoping rules:**
- Distinguish document-level status from provision-level status. An amendment must not automatically invalidate an entire older circular.
- Store the affected provisions or sections whenever the source text identifies them.
- A `REFERENCES` edge alone must never imply supersession.
- If the scope of an amendment or rescission is unclear, record uncertainty (`review_status: needs_review`) and abstain or highlight ambiguity rather than assuming full invalidation.
- Absence of an edge in our incomplete corpus does not prove that a circular remains in force.

## 7. Status and as-of date logic
A status function such as `status_as_of(document_id, date, provision=None)` evaluates validity against the graph and metadata:
- Possible statuses: `in_force`, `amended`, `partially_superseded`, `superseded`, `unknown`.
- **Historical queries:** For questions specifying a historical date, preserve and surface evidence applicable to that requested historical date.
- **Later/current queries:** For questions about a later date, evaluate verified effective dates and supersession evidence chains.
- **Partial supersession retention:** If a circular is only partially superseded, retain relevant unaffected provisions where supported by text and metadata.
- **Effective date discipline:** Do not silently substitute issue date for effective date without recording the assumption. When effective dates or relationships are uncertain, annotate the uncertainty and abstain if it prevents a reliable answer.
- Distinguish "no supersession evidence found in this curated corpus" from "confirmed in force."

## 8. Human review
Automatically extracted graph edges are candidates, not authoritative legal facts. Keep a manually labelled edge set and record:
- evidence sentence
- source page
- relationship label
- scope
- reviewer decision
- reason for correction

Report precision and recall only against a defined, reviewed sample.

## 9. Security and prompt injection
Treat PDF text as untrusted input. It may contain text that resembles instructions. Retrieved content can provide evidence about regulations but must not override system instructions, change tool use, reveal secrets, or control application behavior.

## 10. Reproducibility
Version the manifest, parsing code, chunking parameters, embedding model name/version, index build settings, and graph data. Rebuild indexes from source artifacts rather than treating generated indexes as the only source of truth.
