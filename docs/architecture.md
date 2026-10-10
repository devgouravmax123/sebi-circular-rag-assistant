# System Architecture

## 1. High-level design

```text
                         OFFLINE / ADMIN PIPELINE
Official SEBI PDFs
       |
       v
Download manifest + checksums
       |
       v
PDF text extraction / tables / optional OCR
       |
       v
Metadata + page provenance + quality review
       |
       +----------------------+----------------------+
       |                      |                      |
       v                      v                      v
Chunk records            Supersession graph     Evaluation corpus
       |                 + edge evidence
       v
BM25 index + FAISS dense index
       |
       +---------------------- versioned artifacts ------------------+

                         ONLINE QUESTION PIPELINE
React + TypeScript UI
       |
       | POST /ask
       v
FastAPI validation
       |
       v
BM25 top-k + FAISS top-k
       |
       v
Reciprocal Rank Fusion (RRF)
       |
       v
Optional cross-encoder reranking
       |
       v
Graph-aware status filtering / demotion / related-circular expansion
       |
       v
Context builder + provider-neutral LLM adapter
       |
       v
Structured answer + citation verification + refusal checks
       |
       v
Response: answer / refusal / citations / status / latency
       |
       v
React answer panel + source cards + feedback
```

## 2. Component responsibilities
- **Frontend:** collect question/date, show loading and error states, display citations and disclaimer. It must not contain provider secrets.
- **FastAPI:** validate requests, coordinate retrieval and generation, apply timeouts/rate limits, validate response structure, and expose API contracts.
- **Ingestion:** download or process official PDFs, preserve source URL and retrieval date, extract text per page, and record extraction/OCR quality.
- **Chunker:** create deterministic text chunks with circular ID, date, page range, and chunk ID.
- **BM25:** lexical matching for exact circular IDs, section names, and terminology.
- **FAISS:** dense semantic retrieval over normalized local embeddings.
- **RRF:** combine rankings without assuming raw scores are directly comparable.
- **Reranker:** optional relevance refinement; add only after measuring baseline retrieval.
- **Supersession graph:** store evidence-backed `AMENDS`, `RESCINDS`, and `REFERENCES` edges, including partial/full scope and confidence.
- **LLM adapter:** call the configured hosted provider from the backend; require structured output.
- **Citation validator:** reject unknown chunk IDs, mismatched pages, or citations not present in the retrieved context.
- **Evaluation runner:** run the same dataset against system variants and save machine-readable results.

## 3. Request sequence
1. Validate question and `as_of`.
2. Retrieve lexical (BM25) and dense (FAISS) candidates.
3. Fuse rankings with Reciprocal Rank Fusion (RRF).
4. Optionally rerank candidates if the cross-encoder reranker is enabled.
5. Consult the evidence-backed supersession graph (directed as `Newer/acting circular -> RELATIONSHIP -> Older/affected circular`):
   - Annotate or demote superseded material when relevant to historical context.
   - Exclude material only when its inapplicability to the requested date and scope is sufficiently established.
   - Include relevant amending circulars when they help establish the effective rule.
6. Build a bounded context with source IDs and page metadata.
7. Call the selected LLM provider adapter only if evidence is sufficient and provider configuration is available.
8. Parse structured output.
9. Validate citations and refusal conditions in code before returning an answer.
10. Return response and record safe operational metadata.

## 4. Initial API response shape
```json
{
  "answer": "Answer text supported by the retrieved sources.",
  "refuse": false,
  "refusal_reason": null,
  "as_of": "2026-01-01",
  "citations": [
    {
      "chunk_id": "chunk-example",
      "document_id": "document-example",
      "title": "Example title",
      "issue_date": null,
      "page_start": 2,
      "page_end": 3,
      "source_url": "https://www.sebi.gov.in/"
    }
  ],
  "status_notes": [],
  "latency_ms": 0
}
```
This is a design example only. Example IDs and dates are placeholders, not claims about real circulars.

## 5. Design principles
- Start with a small local vertical slice before corpus-scale ingestion.
- Keep ingestion offline from the user-facing request path.
- Keep provider access behind a backend adapter.
- Keep source provenance attached throughout retrieval and generation.
- Fail closed on citation verification failures.
- Use an explicit `unknown` status when evidence is incomplete.
- Prefer deterministic, testable code for date and graph logic.
- Do not assume free-host local disk is persistent.
- Avoid introducing LangChain, a database, Redis, or complex orchestration unless a measured need appears.

## 6. Scaling and optional components
- **In-process cache:** first choice for a small single-instance demo.
- **Redis:** optional only if distributed cache/rate limiting is needed and a suitable free hosted plan is verified.
- **PostgreSQL:** optional if structured persistence, multi-user feedback, or audit queries justify it.
- **Cross-encoder:** optional; benchmark latency and memory before deployment.
- **Docker/CI:** add after the core flow and tests work.
