# Development Rules for Humans and AI Coding Agents

## 1. Work protocol
1. Read `README.md`, `tasks.md`, and all relevant files in `docs/`.
2. Inspect the current repository before changing it.
3. State the single task being implemented and the expected files to change.
4. Make the smallest coherent change.
5. Run relevant tests and report exact commands and outcomes.
6. Update `tasks.md` and docs when behavior or decisions change.
7. Stop and ask before expanding scope.

## 2. Approval required before
- Replacing the agreed architecture or major technology.
- Adding paid services, enabling billing, or requiring a credit card.
- Adding a new external provider or sending corpus/user data to it.
- Downloading large models or datasets.
- Scraping many documents or running long processes.
- Introducing Redis, a database, a framework, or a new deployment platform.
- Deleting files or rewriting unrelated code.
- Changing API contracts in a breaking way.

## 3. Zero-cost requirement
- No required paid plan, mandatory paid API, or billable resource.
- Check current provider and hosting terms before relying on a free tier.
- Do not claim a service is permanently free or unlimited.
- If free quota is exhausted, show a clear error or use a configured eligible fallback; never silently incur cost.
- Local development must remain possible without public hosting.
- Keep a small fixture corpus for tests so CI does not need to download large PDFs/models.

## 4. Secrets and privacy
- Keep `.env` out of Git.
- Keep `.env.example` free of real secrets.
- Never expose provider API keys in frontend bundles.
- Do not print secrets to logs or include them in screenshots.
- Review the selected provider's current data-use and privacy terms before sending questions or retrieved text.
- Do not log more user content than necessary.

## 5. Code quality
- Prefer typed request/response schemas and small modules with clear responsibilities.
- Keep graph/status logic deterministic and unit-tested.
- Avoid unnecessary abstractions and premature microservices.
- Use stable IDs and reproducible index-building steps.
- Handle provider timeouts, quota failures, malformed outputs, and missing indexes.
- Avoid catching exceptions silently.
- Use clear comments for non-obvious legal-status assumptions.

## 6. Testing
- Add tests for bug fixes and non-trivial behavior.
- Test citation validation, graph direction, partial supersession, date boundaries, missing evidence, and abstention.
- Use synthetic fixtures for unit tests and label them clearly.
- Do not depend on external network access for the core test suite.
- Report failing tests honestly; do not claim a test passed unless it was run.

## 7. Frontend
- Use environment configuration for the backend base URL.
- Provide loading, error, empty, and refusal states.
- Make citations easy to open and inspect.
- Do not render arbitrary retrieved HTML as trusted content.
- Never include LLM API keys in frontend environment variables prefixed for client exposure.

## 8. Completion report format
For every task, report:
- Summary
- Files changed
- Commands/tests run and exact outcomes
- Known limitations
- Next task proposed
