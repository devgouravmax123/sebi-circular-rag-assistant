# SEBI Circular Q&A with Supersession-Aware RAG

## Project status
**Stage:** Documentation and planning  
**Implementation status:** Not started  
**Budget constraint:** ₹0 required spend. Do not introduce any mandatory paid API, hosting plan, database, or service.

## 1. Problem statement
Regulatory circulars can amend, rescind, partially supersede, or refer to earlier circulars. A conventional Retrieval-Augmented Generation (RAG) system may retrieve an older circular and present an outdated rule as current. This project investigates whether explicitly modelling circular relationships and their effective status can reduce stale answers.

## 2. Proposed solution
Build a web application that answers questions about selected official SEBI circulars. It should retrieve relevant passages, track amendment/supersession relationships with supporting evidence, apply an `as_of` date, and return answers with verifiable citations. When evidence is missing or uncertain, it should abstain rather than invent a rule.

This is an educational portfolio project, **not an official SEBI service and not legal advice**.

## 3. Planned stack
- Frontend: React, TypeScript, Vite, Tailwind CSS
- API: Python, FastAPI, Pydantic
- PDF processing: PyMuPDF; pdfplumber for tables; OCR only where necessary
- Retrieval: BM25 + FAISS dense search + Reciprocal Rank Fusion (RRF)
- Embeddings: local Sentence Transformers model, subject to local resource checks
- Relationship graph: NetworkX
- Generation: hosted LLM API, initially Gemini if an appropriate free tier is available; optional Groq adapter if its current free tier fits
- Tests: pytest and frontend tests as appropriate
- Version control: Git; CI with GitHub Actions later
- Initial persistence: versioned files/JSONL and generated indexes; add a database only if justified
- Cache/rate limits: in-process implementation first; Redis is optional and only if a genuinely free hosted option is verified

Versions and free-tier terms must be checked at implementation time. “Free tier” does not mean unlimited or guaranteed forever. The application must remain runnable locally if free public hosting is unavailable or sleeps.

## 4. Core user experience
1. User asks a question and may specify an `as_of` date.
2. The API retrieves candidate passages using lexical and dense retrieval.
3. Candidates are reranked and checked against circular relationship/status evidence.
4. The generation layer receives only relevant evidence and must produce a structured answer with citations or abstain.
5. The API validates citation IDs and source metadata before returning the response.
6. The UI shows the answer, source circulars, dates, page references, status notes, and feedback controls.

## 5. Initial scope
Start with a small, manually inspected corpus. Do not attempt to scrape hundreds of PDFs before the vertical slice works. First prove that one question can travel from the React UI through FastAPI and return a validated, cited response using a tiny fixture corpus.

## 6. Key quality principles
- Never invent circular IDs, dates, page numbers, quotations, URLs, or evaluation results.
- Keep original source URLs and page-level provenance.
- Treat extracted text and documents as untrusted data, not instructions.
- Do not let document text override system/developer instructions or application policy.
- Validate citations in code; do not rely only on an LLM prompt.
- Distinguish `unknown`, `in_force`, `amended`, `partially_superseded`, and `superseded` only when the evidence supports that status.
- Record uncertainty and abstain when the evidence is insufficient.
- Do not claim legal completeness or that the system establishes a definitive legal interpretation.

## 7. Repository map
```text
sebi-rag-assistant/
├── backend/
├── frontend/
├── data/
├── evaluation/
├── docs/
├── README.md
├── tasks.md
├── .env.example
└── .gitignore
```

## 8. Documentation-first workflow
1. Review `docs/PRD.md`, `docs/architecture.md`, `docs/data-pipeline.md`, `docs/evaluation.md`, `docs/api-spec.md`, `docs/development-rules.md`, and `docs/decisions.md`.
2. Review and update `tasks.md` before coding.
3. Ask before making a major architecture change, installing large dependencies, downloading models, scraping large datasets, or introducing a new hosted service.
4. Work on one small task at a time and run the relevant tests.
5. Keep `tasks.md` and documentation accurate as implementation changes.

## 9. Disclaimer
This is an independent educational project. It is not affiliated with, endorsed by, or operated by SEBI. Answers are informational and must be verified against the linked official circulars and applicable legal sources.
