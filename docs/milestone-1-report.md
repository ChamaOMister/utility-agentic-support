# Milestone 1 acceptance evidence

Task 20 verification, 2026-10-22. **Final whole-branch review and controller acceptance are pending.** Task tests passing alone does not complete milestone acceptance. The required lint check currently has two pre-existing import-order findings, retained for the controller's final fix wave: `services/support_api/src/support_api/app.py` and `services/support_api/tests/unit/test_catalog.py` (`I001`).

## Reproducible results

- `.venv/bin/pytest --asyncio-mode=auto --tb=short services/mock_billing services/support_api -v -rs`: **746 passed in 78.57 s; zero failed, zero skipped**. The guard against real OpenAI connections stayed enabled.
- `cd web && npm run build`: passed (Vite 8.3.5).
- `cd web && npx playwright test --workers=2`: **10 passed in 20.5 s**, Chromium, real Compose stack. This covers the offline journey, narrow-screen empty results, lockout, persisted transcript, retry IDs, interruption and 409 handling, visible initialization recovery, and metadata-only drawer.
- Additive-origin runtime override from the README, followed by `docker compose up -d --build` and `scripts/smoke.sh`: four services healthy; Postgres `pg_isready` succeeded, and mock billing, support API, and web returned HTTP 200.
- Fresh isolated Compose project with a new retained database volume, private generated `.env`, and ephemeral loopback ports: `scripts/init-env.sh`, `docker compose up --build -d`, then smoke passed in **5.71 s**. Re-running init correctly refused overwrite; the original working `.env` was byte-identical. Existing volumes were preserved. The isolated containers were stopped after verification; the main demo stack stays running.
- `.venv/bin/ruff check services`: **two existing I001 findings**, listed above; final lint acceptance is pending.

## Acceptance coverage

The combined backend run includes the following checks. This is tested coverage, subject to the pending final review.

| AC | Evidence |
| --- | --- |
| 1 | Fresh-volume startup, generated secrets preserved, dependency healthchecks, bounded smoke |
| 2, 5 | Real-stack browser offline bills/verification journey and three-failure lockout |
| 3, 4 | Billing/executor foreign-bill and identifier-argument rejection; every owned conversation endpoint denies another session |
| 6 | New real-billing API test: punctuation, digits-only and spaced CPF formats share lockout across independent cookies, with no further verify request |
| 7 | New real-billing timeout/500/503 verify tests: one POST, upstream-error outcome, zero failure increments, successful verification after clearing the fault |
| 8 | Ten independent sessions with real billing delayed 500 ms: exactly three wrong verify requests, identifier locked, every reply generic. Real 503 responses held for 500 ms at the test transport: three admitted slots end upstream_error, all counters remain zero, a subsequent correct verification succeeds |
| 9 | New real-billing list-bills timeout/500/503 tests: catalog reply, no fabricated BRL data, trace error and bounded attempts, elapsed at most 10 s (stricter than the 45 s turn cap), followed by successful retry after clearing fault |
| 10 | Support database role receives permission denied on billing schema |
| 11 | Duplicate replay/serialization, conflicting IDs and message lengths |
| 12 | Mocked provider crash after possible send, duplicate/startup recovery, uncertain reservation held, live block |
| 13 | Rev 4 stale-worker pause points listed below, within the documented residual-race limit |
| 14 | Late failure after claim lost and late verification failure/success after takeover, plus `test_apply_verified_after_claim_lost_leaves_assurance`; idempotent outcome and unchanged assurance/transcript checks |
| 15 | Concurrent project/conversation reservation cap checks |
| 16, 17 | Mocked provider classification/retry matrix and token-count parity/failure/deadline tests |
| 18 | Whole-turn truncation, paired tools, interrupted-turn handling and all reasoning replay modes against mocked provider |
| 19 | Closed-gate/zero-request tests; shipped pricing and model entries remain unverified |
| 20 | OpenAI implementation tested against mocked HTTP only; tool round trip, usage reconciliation, retries |
| 21 | Fault endpoints require separate admin credentials and disappear when disabled |
| 22 | New production JSON-formatter capture across all five persona journeys and an outage: correlation IDs, exact metadata allowlist, no names/CPF/account/birth-date/full-phone/street/session-token values |

## Scope of the stale-worker claim

AC 13 is claimed only under **rev 4**: tested pauses are `after_model_response`, `after_reserve`, `after_verify_admit`, and `after_claim_check` with an expired local deadline. The pre-send gate checks the claim and remaining deadline, and subsequent writes are fenced.

The design §5.5 residual race remains open: a whole-process stall of at least 15 s after the local gate passes and before the request completes, or an equivalent database-clock jump, can allow at most one stale request per worker. A generation's reservation stays held/uncertain and blocks live mode; billing GET results are discarded; a late verification outcome counts failures without granting assurance. Receiver-side fencing would be necessary to close it.

## Live OpenAI remains disabled

**No live OpenAI execution, paid call, real key, or provider-account change occurred.** The OpenAI route was tested only against mocked HTTP. All three blocking design §8 prerequisites are **not completed**:

1. Official selected model **and tier** pricing, with source URL and date recorded.
2. Official confirmation that token counting is unbilled, plus its rate limits; billed counting would need a new reservation design.
3. Verified model-specific reasoning replay behavior (`none`, `current_turn`, `all_turns`).

Explicit spending approval is also required before any future live execution. [Provider controls](provider-controls.md) separates monthly hard limits/alerts from the estimated application ledger and describes manual uncertain-row reconciliation. The database stores raw synthetic messages/tool payloads and opaque reasoning for replay; a live model would see typed knowledge answers. The milestone remains restricted to synthetic data.
