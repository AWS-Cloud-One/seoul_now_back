# Recommendation Implementation Plan

> Execute inline with executing-plans and test-driven-development. User approved implementation and OpenAI on 2026-11-05; do not request approval again.

**Goal:** Connect POST /api/v1/chat/messages to OpenAI interpretation, verified Kakao recommendations, continuation, and grounded explanations.
**Architecture:** Typed schemas, pure evidence/ranking functions, injected async provider clients, orchestration service, signed stateless continuation.
**Tech Stack:** Existing FastAPI, Pydantic, HTTPX, PostgreSQL; Python standard-library HMAC. No new dependency.
**Spec:** docs/recommendation-design.md, with OpenAI replacing the unresolved provider.

## Constraints and review focus

Maximum 3 results; ETA limit defaults to 30 minutes; car comparison window 180 seconds. Never substitute car ETA for other transport. Preserve existing APIs. No full chat persistence. Missing/stale traffic or parking is not zero. Test malformed provider data, signed-state tampering/expiry, route-wide outages versus exhaustion, changing origins, and page-limit continuation.

## Tasks

- [x] 1. Add tests for ranking, freshness, nearby geometry and condition patch validation; run RED. Implement app/schemas/recommendation.py, recommendation/ranking.py, evidence.py and ai/interpretation.py; run GREEN.
- [x] 2. Add provider tests for strict Responses JSON, refusal, credentials and Kakao distance sorting; run RED. Implement clients/kakao.py and ai/providers/openai.py, prompts and grounded explanation. Reuse legacy routes through shared client helpers.
- [x] 3. Add /chat/messages sequence tests with mocked external providers, remaining-first paging, duplicates, unsupported modes, signed-state expiry/tampering and condition reset; run RED. Implement recommendation/state.py, services/recommendation.py and routes/chat.py; wire settings and Compose.
- [x] 4. Run whole pytest suite, Ruff and live local endpoint checks. Document configuration, algorithm, prompts, API examples and actual versus mocked verification. Request independent review; fix significant findings and rerun affected checks.

## Decisions / execution ledger

- Keep the user's existing checkout; changes remain uncommitted for review.
- Model default gpt-4.1-mini is configurable, not a claim of latest/best model. Responses API uses store=false.
- Explanation generation chooses validated evidence clauses; server renders numbers and scope. This deliberately prevents ungrounded free-form claims.
- Signed state expires after 30 minutes; default process-local secret means a restart invalidates it. Production workers must share APP_RECOMMENDATION_STATE_SECRET.
- Bound each request to 3 new search pages and 90 seconds; report search_incomplete instead of false exhaustion. Revalidate cached remaining candidates on continuation.
- Final review: independent reviewer found malformed routes misclassified as no-route, malformed search metadata uncaught, and citydata source priority missing. All reproduced with failing tests then fixed; final whole suite 101 passed (including local PostGIS integration), one pre-existing Starlette warning. No deferred findings.
- Local HTTP verification: registered /api/v1/chat/messages; readiness 200; chat without OpenAI key returns explicit 503. OpenAI/Kakao keys absent; external live calls not performed.
- User explicitly prohibited GitHub upload. No commit or push was performed; all changes remain local.
