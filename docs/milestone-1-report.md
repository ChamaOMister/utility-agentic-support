# Milestone 1 acceptance evidence

Task 20 verification, 2026-10-22; fix-wave verification, 2026-10-23. **The final whole-branch review is done: "ready to merge with fixes".** Its required and recommended fixes (I1, I2, M1, M2, M3, M8, M9) were applied in this fix wave, which closes I1's lint and format debt. **Re-review of the fix wave and controller acceptance are pending.** Task tests passing alone does not complete milestone acceptance.

## Reproducible results

Fix-wave run at the fix-wave HEAD, with the Compose stack rebuilt from that HEAD:

- `.venv/bin/pytest --asyncio-mode=auto --tb=short services/mock_billing services/support_api -v -rs`: **754 passed in 58.46 s; zero failed, zero skipped**. Run per service, `.venv/bin/pytest services/support_api -rs` gave **716 passed** and `.venv/bin/pytest services/mock_billing -rs` gave **38 passed**, both with zero skips. The guard against real OpenAI connections stayed enabled.
- `.venv/bin/ruff check services`: **All checks passed**. `.venv/bin/ruff format --check services`: **98 files already formatted**. This repository-root invocation is the canonical gate (see the README).
- `cd web && npm run build`: passed (Vite 8.3.5).
- `cd web && npx playwright test --workers=2`: **12 passed in 36.2 s**, Chromium, real Compose stack. This covers:
  - the offline journey and narrow-screen empty results;
  - lockout and the persisted transcript;
  - retry IDs, interruption and 409 handling;
  - visible initialization recovery and the metadata-only drawer;
  - reloading while a real turn is in progress, held by a persona-scoped billing timeout fault. The test checks the pending indicator, the same-ID recovery that replays the original ID and text, and the reply appearing without a resend.
- Additive-origin runtime override from the README, followed by `docker compose up -d --build` and `scripts/smoke.sh`: four services healthy. Postgres `pg_isready` succeeded, and mock billing, support API and web returned HTTP 200.
- Fresh isolated Compose project (Task 20, not repeated in the fix wave): a new retained database volume, a private generated `.env` and ephemeral loopback ports. `scripts/init-env.sh`, `docker compose up --build -d` and smoke passed in **5.71 s**. Re-running init correctly refused to overwrite, and the original working `.env` was byte-identical. Existing volumes were preserved.

## Acceptance coverage

The combined backend run and the browser suite include the following checks. This is tested coverage, subject to the pending re-review.

| AC | Evidence |
| --- | --- |
| 1 | Fresh-volume startup, generated secrets preserved, dependency healthchecks, bounded smoke |
| 2, 5 | Real-stack browser offline bills/verification journey and three-failure lockout |
| 3, 4 | Billing/executor foreign-bill and identifier-argument rejection; every owned conversation endpoint denies another session |
| 6 | Real-billing API test: punctuation, digits-only and spaced CPF formats share lockout across independent cookies, with no further verify request |
| 7 | Real-billing timeout/500/503 verify tests: one POST, upstream-error outcome, zero failure increments, successful verification after clearing the fault |
| 8 | Ten independent sessions with real billing delayed 500 ms: exactly three wrong verify requests, identifier locked, every reply generic. Real 503 responses held for 500 ms at the test transport: three admitted slots end upstream_error, all counters remain zero, a later correct verification succeeds |
| 9 | Real-billing list-bills timeout/500/503 tests: catalog reply, no fabricated BRL data, trace error and bounded attempts, elapsed at most 10 s (stricter than the 45 s turn cap), followed by a successful retry after clearing the fault |
| 10 | Support database role receives permission denied on the billing schema |
| 11 | Duplicate replay/serialization, conflicting IDs, message lengths, lone-surrogate and NUL text rejected with 422; browser same-ID recovery after a reload mid-turn |
| 12 | Mocked provider crash after a possible send, duplicate/startup recovery, uncertain reservation held, live block |
| 13 | Rev 4 stale-worker pause points listed below, within the documented residual-race limit |
| 14 | Late failure after claim lost and late verification failure/success after takeover, plus `test_apply_verified_after_claim_lost_leaves_assurance`; idempotent outcome and unchanged assurance/transcript checks |
| 15 | Concurrent project/conversation reservation cap checks; `settle()` rejects an all-zero usage |
| 16, 17 | Mocked provider classification/retry matrix and token-count parity/failure/deadline tests |
| 18 | Whole-turn truncation, paired tools, interrupted-turn handling and all reasoning replay modes against a mocked provider; only reasoning is stored from a final response |
| 19 | Closed-gate/zero-request tests; shipped pricing and model entries remain unverified |
| 20 | OpenAI implementation tested against mocked HTTP only: tool round trip, usage reconciliation, retries |
| 21 | Fault endpoints require separate admin credentials and disappear when disabled |
| 22 | Production JSON-formatter capture across all five persona journeys and an outage: correlation IDs, exact metadata allowlist, no names/CPF/account/birth-date/full-phone/street/session-token values. Unknown model-supplied tool names are recorded as `unknown` in the trace, the `tool_calls` row and the log |

## Scope of the stale-worker claim

AC 13 is claimed only under **rev 4**. The tested pauses are `after_model_response`, `after_reserve`, `after_verify_admit`, and `after_claim_check` with an expired local deadline. The pre-send gate checks the claim and remaining deadline, and every later write is fenced.

The design §5.5 residual race remains open. A whole-process stall of at least 15 s after the local gate passes and before the request completes, or an equivalent database-clock jump, can let at most one stale request per worker go out. The effects are:

- a generation's reservation stays held or uncertain and blocks live mode;
- billing GET results are discarded;
- a late verification outcome counts failures without granting assurance.

Closing the race would require receiver-side fencing.

## Browser and environment limits

Browser verification ran in Chromium only. **Firefox was not exercised.** A **truly fresh Codespace was not provisioned**: fresh-volume verification covers initialization and health, and the UI journeys ran on the normal stack.

## Live OpenAI remains disabled

**No live OpenAI execution, paid call, real key, or provider-account change occurred.** The OpenAI route was tested only against mocked HTTP. These prerequisites for opening the live gate are **not completed**:

1. Official selected model **and tier** pricing, with source URL and date recorded (design §8).
2. Official confirmation that token counting is unbilled, plus its rate limits. Billed counting would need a new reservation design (design §8).
3. Verified model-specific reasoning replay behavior (`none`, `current_turn`, `all_turns`) (design §8).
4. A single source of truth for `PROJECT_BUDGET_USD`. The support migration reads it once to seed the project account's cap. `Settings.project_budget_usd` exists but nothing reads it, and Compose does not forward the variable.
5. Database `CHECK` constraints on the ledger's money columns.

Explicit spending approval is also required before any future live execution. [Provider controls](provider-controls.md) separates monthly hard limits and alerts from the estimated application ledger, and describes manual reconciliation of uncertain rows. The database stores raw synthetic messages, tool payloads and opaque reasoning for replay, and a live model would see typed knowledge answers. The milestone remains restricted to synthetic data.

## Known deferred minors

The final review triaged these as safe to defer. They are not fixed:

- **M4.** mock-billing logs plain text on stderr, outside the JSON allowlist pipeline. Access logs are off and no payloads or path parameters are logged.
- **M5.** Scripted call IDs can collide after token-budget truncation drops an older tool turn. Scripted mode only.
- **M6.** The scripted model asks for credentials again once the verifying turn leaves the six-turn history window, although server assurance is still valid.
- **M7.** An expired session cookie mid-page gives a dead end (`ui.unavailable`, no retry) until a manual reload.
