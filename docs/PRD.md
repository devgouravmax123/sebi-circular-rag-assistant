# Product Requirements Document (PRD)

## 1. Product summary
An independent educational web application for asking questions about a curated set of official SEBI circulars. The distinguishing feature is explicit modelling of amendment, rescission, and reference relationships to reduce answers based on outdated documents.

## 2. Problem
A standard RAG pipeline can retrieve semantically relevant but outdated material. If it does not model document chronology and legal relationships, it may answer using a circular that has been amended or rescinded.

## 3. Goals
- Return evidence-grounded answers with source circular and page citations.
- Support an optional `as_of` date.
- Track supersession relationships with evidence and uncertainty.
- Abstain when evidence is insufficient or citations cannot be verified.
- Measure whether supersession-aware retrieval reduces stale answers compared with baselines.
- Keep the required operating cost at ₹0.

## 4. Non-goals
- Not an official SEBI product.
- Not legal advice or a replacement for reading the original circular.
- Not a guarantee that the curated corpus is complete or current.
- Not automated legal adjudication.
- Not an initial production-scale, multi-tenant SaaS.
- Not dependent on Redis, a paid database, a paid LLM, or a paid host.

## 5. Primary users and stories
1. As a learner, I can ask a question about a circular and see the answer's sources.
2. As a user, I can select an `as_of` date to ask what the indexed evidence supports for that date.
3. As a user, I can open the official source and inspect the cited page.
4. As a user, I can see when the system cannot answer confidently.
5. As a project evaluator, I can compare baseline RAG with supersession-aware RAG using a repeatable evaluation set.
6. As a maintainer, I can trace each graph edge to an evidence sentence and page.

## 6. Functional requirements
- FR-1: accept a text question with maximum initial length of 500 characters.
- FR-2: accept optional `as_of` date.
- FR-3: retrieve candidate passages using lexical and dense search.
- FR-4: combine rankings using Reciprocal Rank Fusion.
- FR-5: rerank candidates when the reranker is enabled.
- FR-6: evaluate circular relationships/status with evidence.
- FR-7: return structured answer, refusal state, citations, status notes, and latency.
- FR-8: validate that each citation refers to a known retrieved chunk.
- FR-9: expose health and feedback endpoints.
- FR-10: log useful diagnostics without secrets or unnecessary personal data.

## 7. Non-functional requirements
- Correctness and traceability take priority over fluent but unsupported answers.
- API keys must never be sent to the browser.
- Source provenance must be preserved.
- The project must have a documented local setup.
- Free-tier limits, cold starts, model downloads, and quotas must be documented.
- Errors should be understandable and must not expose secrets or stack traces to users.
- Evaluation results must be reproducible and not fabricated.

## 8. Acceptance criteria
- A user can submit a question through the UI.
- The API returns a valid structured response.
- Every displayed citation maps to a known source and page.
- An unanswerable question can produce a clear refusal.
- Supersession decisions expose their supporting evidence and uncertainty.
- Tests cover critical status logic and citation validation.
- Baseline and supersession-aware systems can be compared using the same held-out questions.
- No required paid service is introduced.

## 9. Risks
- Missing or ambiguous amendment language.
- Partial supersession that cannot be represented as a simple whole-document status.
- Poor PDF extraction, especially tables and scanned pages.
- Free LLM quota changes or provider data-use terms.
- Public hosting sleeping, losing local files, or imposing resource limits.
- An evaluation set too small to support broad claims.

## 10. Product disclaimer
Display a persistent disclaimer: independent educational project; not affiliated with or endorsed by SEBI; not legal advice; verify answers against the linked official source.
