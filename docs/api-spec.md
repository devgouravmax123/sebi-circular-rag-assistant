# API Specification (Initial Draft)

## General
- Base path: `/`
- Content type: JSON
- API keys for external model providers are backend-only environment variables.
- All error responses should use a consistent shape and avoid exposing stack traces.

## `GET /health`
Purpose: basic process health.

Example response:
```json
{"status": "ok"}
```
A later readiness check may separately report whether indexes and provider configuration are available. Do not report “ready” when required components are missing.

## `POST /ask`
Purpose: retrieve evidence and generate a cited answer.

Example request:
```json
{
  "question": "What does the selected circular say about ...?",
  "as_of": "2026-01-01"
}
```
`as_of` is optional and must be a valid ISO date when supplied. Initial maximum question length: 500 characters.

Example response:
```json
{
  "answer": "Answer supported by the cited evidence.",
  "refuse": false,
  "refusal_reason": null,
  "as_of": "2026-01-01",
  "citations": [
    {
      "chunk_id": "known-chunk-id",
      "document_id": "known-document-id",
      "title": "Source title",
      "issue_date": null,
      "page_start": 1,
      "page_end": 2,
      "source_url": "official-source-url"
    }
  ],
  "status_notes": [],
  "latency_ms": 0
}
```
Values above illustrate schema only; they are not factual circular data. Example IDs and dates are placeholders.

### Citation requirements
- `chunk_id` and `document_id` must exist in the current corpus/index.
- `title`, `page_start`, `page_end`, and `issue_date` (nullable) must come from chunk/document metadata.
- `source_url` must come from the trusted ingestion manifest.
- A citation must be retrieved and available in the prompt context.
- If citation validation fails, return a refusal (`refuse: true`) or safe error instead of an unverified answer.

### Refusal Rubric (`refuse: true`)
The API returns `refuse: true` with a clear `refusal_reason` when:
1. **Insufficient evidence:** No retrieved chunks meet the minimum relevance threshold for the question.
2. **Citation validation failure:** Returned claims or citation IDs cannot be verified against the retrieved context chunks.
3. **Unresolvable conflicting evidence:** Extracted passages or provisions contain irreconcilable contradictions.
4. **Out of domain:** The question falls outside the scope of the indexed circular corpus.
5. **Graph / temporal uncertainty:** Supersession scope, edge confidence, or effective date ambiguities prevent a reliable answer for the requested `as_of` date.

## `POST /feedback`
Purpose: accept simple user feedback about a response.

Example request:
```json
{
  "request_id": "opaque-generated-id",
  "rating": "helpful",
  "comment": null
}
```
Validate input and limit comment length. Do not require name, email, or other personal data for initial feedback. Avoid storing raw sensitive question content unless there is a clear need and documented retention policy.

## Error behavior vs. Refusal behavior
**Important Distinction:**
- **Refusal (`refuse: true`, HTTP 200):** A valid domain response when evidence is missing, ambiguous, or superseded as per the refusal rubric.
- **Infrastructure Error (HTTP 4xx / 5xx):** Occurs on provider quota exhaustion (HTTP 429), upstream LLM timeout (HTTP 504), service unavailability (HTTP 503), or invalid input syntax (HTTP 422). Infrastructure failures must return explicit HTTP error codes and safe messages; they must never be disguised as successful refusals or answers.
- User-facing error messages must never expose API keys, secret prompts, raw stack traces, or internal filesystem paths.

## Future additions
Streaming, authentication, multi-user saved history, and admin ingestion endpoints are out of scope until the basic flow is working and there is a justified requirement.
