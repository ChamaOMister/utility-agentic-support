# Milestone 1 — Chat Vertical Slice: Design Spec

- **Status:** Draft, revision 4 (addresses reviews of 2026-10-22; see §9)
- **Date:** 2026-10-22
- **Scope:** Milestone 1 of `utility-agentic-support` (chat: authentication → bill lookup → useful response)

## 1. Context

`utility-agentic-support` is a public portfolio project demonstrating an AI customer-support agent for a
fictional Brazilian electricity provider, **Vértice Energia (fictional company)**. All customers, bills and
policies are synthetic. Customer-facing text is Brazilian Portuguese (PT-BR); code and developer docs are English.

Roadmap (each milestone independently demonstrable):

1. **Chat vertical slice** — this spec.
2. Knowledge ingestion, grounded answers, structured evaluations.
3. Confirmed actions (step-up verification, service requests, second copy), failure handling, local handoff simulator.
4. Integration with Project 2 (Contact Center Agent Guidance) using its actual interfaces.
5. Voice (cascaded STT + LLM + TTS by default) using the same core.
6. Telephony feasibility, demo polish, documentation.

### 1.1 Decisions already made

| Topic | Decision |
|---|---|
| Model provider | OpenAI. Model configurable via `OPENAI_CHAT_MODEL`. Proposed default `gpt-6-luna` (docs list function calling; structured-output support to be confirmed before relying on it). Step-up candidate `gpt-6.1-sol`. |
| Spending | US$20 proposed *cumulative* development budget for application API usage (text, embeddings, STT, TTS), excluding Codespaces and coding-assistant costs. **Paid calls disabled until the owner explicitly approves and configures a key.** |
| Voice (later) | Cascaded by default; strict channel/business-logic boundary so realtime speech-to-speech can be evaluated later. Latency to be measured, not assumed. |
| Authentication | Demo knowledge-based check: identifier (CPF or account number) as lookup hint + two **knowledge checks** (date of birth, last 4 digits of registered phone). Not two factors. Synthetic data only. Session model ready for step-up (Milestone 3). |
| Billing data | Separate mock billing REST service with its own schema and DB role; reached only via typed HTTP client. |
| Retrieval infra | No pgvector in Milestone 1. |
| Runtime | Python 3.12 (Codespace ships 3.14; 3.12 chosen for native-wheel compatibility, to be verified at install). |
| Project 2 | Handoff contract not yet provided; out of scope for Milestone 1. |

## 2. Goals and non-goals

**Goals:** an independently runnable stack in Codespaces where a customer chats in PT-BR, is asked to verify
only when customer data is needed, and receives an accurate summary of their account and bills — with backend
authorization, bounded execution, deterministic failure handling, cost controls, and redacted observability.
Everything works offline (no API key) via a clearly labeled scripted model.

**Non-goals (Milestone 1):** RAG and pgvector, knowledge documents, second-copy links, state-changing actions,
step-up verification, human handoff, voice, evaluation datasets, streaming responses, production deployment.

## 3. Architecture

```
Browser (Vite/React, PT-BR UI)
   │  same-origin /api/* (Vite proxy) — opaque session cookie
   ▼
support-api (FastAPI, schema `support`, role `support_app`)
   channels/chat ──► AgentCore.handle_turn(conversation, text, channel)
                        ├─ agent/      bounded loop, prompts, history assembly, ChatModel (OpenAI | Scripted)
                        ├─ tools/      registry + executor (validate → authorize → run → typed result)
                        ├─ identity/   knowledge checks, attempt slots, lockout, assurance, verification expiry
                        ├─ turns/      turn claim, fencing, crash recovery
                        ├─ usage/      token counting, budget reservations + ledger (estimates)
                        └─ integrations/billing/  typed httpx client
   │  HTTP + X-API-Key + X-Correlation-ID
   ▼
mock-billing (FastAPI, schema `billing`, role `billing_app`)  ← system of record for customers/bills
   │
   ▼
PostgreSQL 16 (one instance; `support_app` has NO grants on schema `billing`)
```

### 3.1 Repository layout

```
docker-compose.yml          db, mock-billing (:8001), support-api (:8000), web (:5173)
.devcontainer/              python:3.12 devcontainer image + Node + docker-in-docker features
.env.example                placeholders only; scripts/init-env.sh generates local random secrets
db/init/                    creates roles, schemas, grants, test databases
data/synthetic/             versioned synthetic personas (seed source for mock-billing; demo panel source)
services/mock_billing/      own pyproject, Alembic (schema billing), seed data, tests
services/support_api/       own pyproject, Alembic (schema support), prompts/, config/pricing.json, tests
web/                        Vite + React (JavaScript), Playwright tests
docs/                       specs, ADRs, API docs, troubleshooting
```

Each Python service runs its own Alembic migrations with its own role. The browser talks only to the `web`
origin; Vite proxies `/api` to `support-api`. This avoids cross-site cookie problems between Codespaces
forwarded-port hostnames and keeps the session cookie same-origin.

### 3.2 Channel boundary

`channels/chat` handles HTTP specifics (cookie, request/response shapes, status codes) and calls
`AgentCore.handle_turn`. All authorization, tool execution, limits, and budget logic live behind that call.
Voice (Milestone 5) will be another adapter calling the same method.

## 4. Mock billing service

All `/v1` endpoints require `X-API-Key` (constant-time comparison). `X-Correlation-ID` is accepted and logged.

| Method & path | Purpose | Notes |
|---|---|---|
| `POST /v1/identity/verify` | Check identifier + knowledge answers | Body `{identifier, birth_date, phone_last4}` → `{verified: bool, customer_ref?, first_name?}`. Unknown identifier and wrong answers return the identical `{verified: false}` with 200. |
| `GET /v1/customers/{ref}/account` | Account summary | Masked CPF (`***.***.***-12`), installations, address, tariff class, account status. |
| `GET /v1/customers/{ref}/bills?status=&limit=` | List bills | Newest first; `status ∈ {open, paid, overdue}`; `limit ≤ 12`. |
| `GET /v1/customers/{ref}/bills/{bill_id}` | Bill detail | 404 if the bill does not belong to `ref` (indistinguishable from nonexistent). |
| `PUT /_dev/faults`, `DELETE /_dev/faults` | Deterministic fault rules | Only mounted when `FAULT_INJECTION_ENABLED=true` (default `false`; enabled only by the dev Compose override and tests). **Always authenticated:** require `X-Fault-Admin-Key` matching `FAULT_ADMIN_KEY` (constant-time; a separate secret from the service API key, which support-api holds and never needs for faults). Missing/wrong key → 401. Rules match `(route_name, customer_ref?)` → `timeout` \| `http_500` \| `unavailable` (503) \| `delay_ms:N`. |
| `GET /health` | Liveness | No auth. |

The billing service trusts the caller (service-to-service) to pass only verified `customer_ref`s; ownership of
bills is enforced by path scoping.

### 4.1 Synthetic data

Five customers, each with an account number and a generated CPF (valid check digits) used solely as a
synthetic fixture, names obviously fictional, and 6–12 monthly bills (reference month, issue date, due date,
amount in BRL as `Decimal`, status, consumption kWh, tariff flag *bandeira verde/amarela/vermelha*, line items):

| Persona | Situation |
|---|---|
| A | One open + one overdue bill |
| B | All bills paid |
| C | Two installations (home + small shop) |
| D | Recent consumption spike (seeds Milestone 2 bill-dispute scenario) |
| E | Used for outage demos via fault rules |

## 5. support-api

### 5.1 Browser sessions and conversation ownership

- On first request without a valid cookie, the API creates a browser session: 256-bit random token in an
  `HttpOnly; Secure; SameSite=Lax; Path=/api` cookie (`uas_session`); only its SHA-256 hash is stored.
  Session lifetime 8 h absolute.
- Every conversation belongs to exactly one browser session. Every conversation endpoint loads the
  conversation **by `(conversation_id, browser_session_id)`**; a mismatch returns 404, identical to nonexistent.
  Knowing a `conversation_id` grants nothing.
- State-changing endpoints require `Content-Type: application/json` and an `Origin` matching the configured
  allowed origin(s) (CSRF defense in addition to SameSite).

**Allowed-origin configuration.** `ALLOWED_ORIGINS` is a comma-separated list of exact origins
(scheme + host + port; no wildcards, no trailing slash). The documented values are:

| Environment | Value |
|---|---|
| Local machine | `http://localhost:5173` |
| Codespaces | `https://${CODESPACE_NAME}-5173.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}` |

`scripts/init-env.sh` writes the Codespaces origin when `CODESPACE_NAME` is set and the localhost origin
otherwise. Vite's `server.allowedHosts` must include the Codespaces hostname. The `/api` proxy rewrites only
`Host` (`changeOrigin`), never `Origin`, so support-api checks the browser's real origin. The troubleshooting
doc covers the symptom of a mismatch (403 `origin_not_allowed` on every POST).

### 5.2 Identity, assurance, lockout

- Assurance levels per conversation: `none` → `kba` (Milestone 1) → `step_up` (reserved for Milestone 3).
- Verified state (`customer_ref`, `assurance_level`, `verified_at`, `verified_expires_at`) is stored server-side
  on the conversation. Verification expires 30 minutes after `verified_at`; after expiry the level is `none`
  and data tools return `authentication_required`.
- **Identifier normalization:** strip everything but digits; classify as CPF (11 digits) or account number
  (10 digits); anything else is `invalid_arguments` (no upstream call, not a failed attempt). The lockout key
  is `HMAC-SHA256(IDENTIFIER_HASH_KEY, kind + ":" + digits)`.
- **Lockout:** 3 failed attempts within 15 minutes lock (a) the identifier hash, across all conversations and
  sessions, and (b) the conversation itself, for 15 minutes.
- **What counts as a failure:** only an upstream response of `{verified: false}`. Timeouts, 5xx, and
  unavailability are **not** counted and yield an `upstream_*` error.
- **Atomic admission via attempt slots.** No verification request is sent without first reserving an
  attempt slot, so concurrent conversations cannot all pass a precheck before any failure is recorded:
  1. *Admit* (one transaction, before any upstream call): upsert the `identifier_lockouts` row and lock it
     (`INSERT … ON CONFLICT DO NOTHING`, then `SELECT … FOR UPDATE`), which serializes admission per
     normalized identifier. Reset the window if expired. Reject if `locked_until > now()`. Reject if
     `failed_count + (unexpired in-flight slots for this hash) >= 3`. Otherwise insert a
     `verification_attempts` row (`status=in_flight`, `expires_at = now() + 30 s`) and commit. The same
     check runs against the conversation: reject if its `locked_until > now()` or if its
     `failed_attempts + (unexpired in-flight slots for this conversation) >= 3`.
  2. *Call* `POST /v1/identity/verify` (POST, never retried; 3 s timeout, capped by the remaining deadline).
  3. *Record outcome* with the attempt-outcome transaction below.
- **Attempt-outcome transaction.** One idempotent transaction, keyed by attempt ID, records every
  verification outcome. It is the only path that changes failure counters:
  - It begins with `UPDATE verification_attempts SET status=:outcome WHERE id=:attempt_id AND status IN
    ('in_flight', 'abandoned') RETURNING identifier_hash, conversation_id`. Zero rows means the outcome was
    already recorded, and the transaction does nothing else, so a replayed outcome never double-counts.
  - `{verified: false}` → status `failed`. Increment **both** the identifier's `failed_count` and the
    conversation's `failed_attempts` (window-aware), and set `locked_until` on each that reaches the threshold.
  - Timeout/5xx/connection error → status `upstream_error`. No counter changes.
  - `{verified: true}` → status `verified`. No counter changes.
  - It is **not fenced** by the turn claim (§5.5). A worker that lost its claim still records its outcome,
    so a conversation with two failures cannot escape its third after recovery. It writes only the attempt
    row and the failure-counter columns (`failed_count`/`failed_attempts`, `window_started_at`,
    `locked_until`). It never writes assurance (`customer_ref`, `assurance_level`, `verified_*`), messages,
    conversation items or turn status.
- **Applying a successful verification** is a fenced write. The worker first tries one transaction that
  passes the claim check (§5.5) and then applies both the outcome and the verified state. If the claim check
  fails, it runs the outcome transaction alone: the attempt is recorded as `verified`, but the
  conversation's assurance does not change.
- All transactions in §5.2 and §5.5 lock rows in a fixed order (`conversations` → `verification_attempts` →
  `identifier_lockouts` → `conversation_auth`), skipping tables they do not touch.
- Slot expiry (30 s, well above the 3 s verify timeout) bounds how long a crashed worker can hold a slot.
  Expired slots no longer count toward admission. Failure increments are never dropped: a `{verified: false}`
  that arrives after its slot expired or was marked `abandoned` still increments both counters.
- Admission rejected for capacity (slots full, not yet locked) returns `ToolError verification_busy`, which
  is not a failed attempt.
- All authentication failures, lock states and `verification_busy` produce the same generic PT-BR message
  (catalog in §5.8).

### 5.3 Tools

Exactly one tool accepts an identifier: `authenticate_customer`. The three customer-data tools take **no
customer identifier**; the executor injects `customer_ref` from the verified session.

| Tool | Input | Output | Required assurance |
|---|---|---|---|
| `authenticate_customer` | `identifier: str`, `birth_date: date`, `phone_last4: str (4 digits)` | `{status: verified, first_name}` or typed error | none |
| `get_account_summary` | — | installations, tariff class, account status (no full address to the model beyond neighborhood/city) | kba |
| `list_bills` | `status?: open\|paid\|overdue`, `limit?: 1..12 (default 6)` | list of bill summaries | kba |
| `get_bill_details` | `bill_id: str` | bill detail incl. line items | kba |

**Executor pipeline:** parse args (Pydantic, `extra="forbid"`) → assurance check → attempt-slot admission
(auth tool, §5.2) → handler under per-tool deadline → typed `ToolResult` or `ToolError`. When a model response
contains several tool calls, the executor runs them sequentially in the order given.

`ToolError.code ∈ {invalid_arguments, authentication_required, authentication_failed, locked,
verification_busy, not_found, upstream_timeout, upstream_unavailable, internal_error}`. Results are sent to
the model wrapped as `{"untrusted_data": …, "status": …}`; the system prompt states that tool data is data,
never instructions, and that the model must never claim an outcome a tool did not report.

### 5.4 Agent loop and limits

| Limit | Default |
|---|---|
| Model calls per turn | 5 |
| Tool calls per turn | 4 |
| Turns per conversation | 30 |
| Max output tokens per model call | 800 |
| Turn total deadline | 45 s (includes token counting, generation, tools) |
| LLM per-attempt timeout | 20 s (capped by remaining turn deadline) |
| Token-count timeout | 10 s (capped by remaining turn deadline); no retry |
| LLM retries | ≤ 1, only after a connection failure before send or a transient rate-limit 429 (§5.6) |
| Billing per-attempt timeout | 3 s |
| Billing total deadline per tool call (incl. backoff) | 8 s |
| Billing retries | ≤ 2, GET only, backoff 200 ms / 600 ms with jitter; on timeout, 5xx, connection error |
| History sent to model | ≤ `HISTORY_MAX_TURNS` (6) most recent complete turns + current turn, within `MAX_INPUT_TOKENS` (§5.4.1) |

Every timeout is `min(per_attempt, remaining_deadline)`. When a limit is reached, the turn ends with a
deterministic PT-BR message offering a next step (retry later); it never claims success. Retry logic exists
in exactly one place per integration (billing client; LLM call wrapper). SDK-level retries are disabled
(`max_retries=0`).

#### 5.4.1 History assembly

- Every model-visible item is persisted in `conversation_items` (`turn_id`, `seq`, `kind ∈ {user_message,
  assistant_message, function_call, function_call_output, reasoning}`, `call_id`, producing `model`, payload).
  `reasoning` payloads are the provider's opaque item, including `encrypted_content`. They are never logged
  or shown.
- The unit of history is the **complete turn**. A completed turn contributes all of its items in order. Every
  `function_call` comes with its `function_call_output` (same `call_id`), and vice versa. An `interrupted`
  or failed turn (§5.5) contributes only its user message and its deterministic catalog reply. Its tool items
  are omitted, so a dangling call never reaches the model.
- Input = system instructions + up to `HISTORY_MAX_TURNS` most recent complete turns + all items of the
  current turn. Truncation removes **whole turns, oldest first**. It never removes part of a turn or part of
  a tool exchange. The current turn is never truncated. If the current turn alone cannot fit within
  `MAX_INPUT_TOKENS`, the turn ends with the limit message and no request is sent.
- Selection uses a local size estimate to choose which turns to include. The authoritative check is the
  provider token count (§5.6). If that count exceeds `MAX_INPUT_TOKENS`, the oldest included turn is dropped
  and the request is counted once more. If it still exceeds, the turn ends with the limit message.
- Before every count or generation request, an invariant check confirms that each `function_call_output` is
  preceded by a `function_call` with the same `call_id` and that each `function_call` has an output (except
  calls awaiting execution in the current turn). A violation is `internal_error` and no request is sent.
- **Reasoning items.** Per OpenAI's reasoning guide, stateless (`store=False`) responses return reasoning
  items carrying `encrypted_content` for replay.
  - Within the current turn, every reasoning, function-call and function-call-output item since the user
    message is passed back on each follow-up call.
  - **Storage (retention):** every reasoning item is persisted and stays attached to its turn, whatever the
    model setting. Stored items are never removed or detached individually.
  - **Request (filtering):** for each request, assembly first selects whole turns (truncation drops a turn
    with all of its stored items, reasoning included). It then filters reasoning items out of the request
    according to the model's verified `reasoning_items` setting in `config/models.json` (§5.6 gate):
    - `none`: the model emits none.
    - `current_turn`: reasoning items from earlier turns are left out of the request, though they remain
      stored.
    - `all_turns`: every output item of each included turn is sent, reasoning included.
  - Reasoning items produced by a model other than the configured one are also left out of the request.
    Filtering never changes what is stored.

### 5.5 Turns, duplicate submissions, serialization, crash recovery

- `POST /api/conversations/{id}/messages` with `{client_message_id: uuid, text: str (≤ 2,000 chars)}`.
- `turns` has `UNIQUE(conversation_id, client_message_id)` and stores `sha256(text)`.
  - Same id + same text, turn `completed` or `interrupted` → return the stored reply and status (no new
    model/tool calls).
  - Same id + same text, turn `in_progress` with an unexpired lease → `409 turn_in_progress`.
  - Same id + same text, turn `in_progress` with an expired lease → run recovery (below), then return the
    stored `interrupted` reply.
  - Same id + different text → `422 client_message_id_conflict`.
- **Claim.** Turns are serialized per conversation by a conditional
  `UPDATE conversations SET active_turn_id=:t, claim_version=claim_version+1,
  active_turn_lease_expires_at=now()+60s WHERE id=:c AND (active_turn_id IS NULL OR lease expired)
  RETURNING claim_version`. A concurrent different message gets `409 turn_in_progress`. The lease (60 s)
  exceeds the turn deadline (45 s).
- **Fencing.** The worker holds `(turn_id, claim_version)` as its fencing token. Every state write by a turn
  worker runs in a transaction that begins with `SELECT … FROM conversations WHERE id=:c AND
  active_turn_id=:t AND claim_version=:v FOR UPDATE`. This covers messages and conversation items, tool-call
  rows, verified state, budget reservations, turn completion, and lease release. Zero rows means the claim
  was lost: the transaction rolls back, the worker raises `ClaimLost`, and it stops without writing anything
  or sending any further provider or billing request (enforced by the pre-send gate below, within its stated
  limit). A paused worker that resumes after takeover therefore
  cannot overwrite the new owner's state. Only two writes are unfenced, and both are keyed by their own row,
  not by conversation state:
  - The attempt-outcome transaction (§5.2). It writes the attempt row and failure counters only, never
    assurance or transcript.
  - Settlement of the worker's own `llm_calls` row with usage it actually received (`reserved|uncertain →
    settled`). It never releases a held amount without reported usage.
- **Pre-send gate and its limit.** Fencing protects database writes. It can't stop an outbound request,
  because neither OpenAI nor the billing API checks the fencing token. Immediately before every outbound request
  (token count, generation, each billing attempt, each retry), the worker runs a pre-send gate. The gate
  re-checks the claim in the database, then checks locally, with no I/O, that the turn deadline has not
  expired. The request is sent at once, and its total wall-clock time is capped by the remaining deadline.
  The deadline (45 s) starts after the claim, and the lease (60 s) starts at the claim. So a worker that
  pauses anywhere before the gate is always stopped: by the claim check if a takeover has happened, or by the
  deadline check otherwise. The **residual race** is a whole-process stall of at least 15 s
  (lease − deadline) after the gate passes and before the request completes, such as a SIGSTOP, a VM suspend
  or a blocked event loop. A DB clock jump of the same size has the same effect. In that case at most one
  stale request per worker can be sent, or still be in flight, after takeover. Its effects are bounded:
  - a generation's reservation stays `reserved`, so recovery marks it `uncertain` and blocks live generation;
  - a billing GET is read-only, and its result is discarded because every later write is fenced;
  - a verify POST's outcome goes through the attempt-outcome transaction, which counts failures and never
    grants assurance.

  Milestone 1 does not close this race.
- **Recovery of abandoned turns.** Recovery runs whenever a request touches a conversation whose active turn
  has an expired lease (message submit, duplicate replay, transcript read). On startup, support-api also
  sweeps every conversation in that state. In one transaction, locking the conversation row:
  1. Confirm the lease is still expired; increment `claim_version` (fences the old worker).
  2. Mark the abandoned turn `interrupted` and store the catalog reply `turn_interrupted`.
  3. For each of that turn's `llm_calls` rows still `reserved` (a request may have been sent): move it to
     `uncertain`, keep the full reservation, and set `conversations.live_blocked_reason =
     'interrupted_after_possible_send'`. Live generation is then blocked for that conversation. Scripted
     mode is unaffected.
  4. Mark that turn's `in_flight` verification slots `abandoned`, which frees their capacity without
     counting failures. A late outcome is still recorded by the attempt-outcome transaction (§5.2), and a
     late `{verified: false}` still increments both the identifier and the conversation counters.
  5. Clear `active_turn_id`. If the triggering request is a new message, the claim proceeds in the same
     transaction.
- The client treats an `interrupted` reply as final for that `client_message_id`. Its retry button submits
  the text again with a new `client_message_id`.

### 5.6 Models and spending guard

- `ChatModel` protocol, two implementations:
  - `ScriptedModel` (default, `LLM_PROVIDER=scripted`): deterministic, rule-based PT-BR model that drives the
    demo journey through the **real** executor and billing client. UI shows *"Modo offline — respostas simuladas"*.
    It never sends provider requests, including token counts.
  - `OpenAIResponsesModel` (`LLM_PROVIDER=openai`): Responses API with function tools, `store=False`,
    `max_output_tokens`, an explicit `service_tier`, explicit timeout, `max_retries=0`.
- Tests additionally use a queued-response fake for edge cases.

**Live-mode gate.** The OpenAI client is constructed, and any provider request is sent (token counts
included), only when **all** hold. Otherwise startup fails fast with a clear message:
1. `LIVE_LLM_ENABLED=true` and `OPENAI_API_KEY` is set (owner approval of spending).
2. `config/pricing.json` has a **verified** entry for `(OPENAI_CHAT_MODEL, OPENAI_SERVICE_TIER)`: input and
   output rates per million tokens, `source_url`, `verified_on` date, `verified: true`. Placeholder or
   unverified entries do not satisfy the gate.
3. Token counting billing is **recorded**: `pricing.json` has `token_count: {billed: true|false,
   source_url, verified_on}`. Unknown (missing, non-boolean or without provenance) → gate closed.
   `billed: false` sends counts with no ledger row. `billed: true` (rev 5, the shipped setting, because
   OpenAI does not document the endpoint as free) gives every count its own `llm_calls` row of kind
   `token_count`:
   - **Reserve** before sending, in one fenced transaction against both caps, an upper bound of one input
     token per UTF-8 byte of the JSON count body plus 1,000 tokens, priced at the uncached input rate. If a
     cap would be exceeded, no count is sent and the turn ends with the live-blocked message.
   - **Pre-send gate** as for generation. A lost claim keeps the row `reserved` for recovery (§5.5 step 3
     moves it to `uncertain` and blocks). Any other failure before sending releases the hold.
   - **Settle** a successful count (`input_tokens ≥ 1`) at the input rate for the reported tokens. A zero or
     malformed count is a malformed response.
   - **Failures after a possible send**: connect failures (never sent) release. 429 quota/billing and
     400/401/403 leave the hold `uncertain` and block live mode (`provider_quota` / `provider_rejected`), as
     for generation. 5xx, timeouts, rate limits and malformed results leave the hold `uncertain` without
     blocking: only that turn ends, and the operator reconciles the row (see provider controls).
4. `config/models.json` has a verified entry for `OPENAI_CHAT_MODEL` giving its `reasoning_items` setting
   (`none` | `current_turn` | `all_turns`, with `source_url` and `verified_on`; §5.4.1).

These checks are exactly the "blocking for enabling live generation" list in §8. Adding a prerequisite
there means adding a gate check here.

**Per-attempt sequence (live mode).** A single builder produces one request object. Generation sends it
in full. The token count sends the same object minus generation-only fields, explicitly listed in code:
`max_output_tokens`, `store`, `service_tier`, and any other field the count endpoint rejects. A unit test
asserts that both requests carry identical `model`, `instructions`, `input`, `tools` and `tool_choice`.

1. **Assemble** history (§5.4.1) and run the tool-exchange invariant check.
2. **Count** with `POST /v1/responses/input_tokens`. Per OpenAI's token-counting guide, the count includes
   formatting tokens that local tokenization misses. The count call is subject to the turn deadline and is
   never retried. If it fails for any reason (error, timeout, malformed response), the turn ends with the
   upstream-unavailable message and **no generation request is sent**. If the count exceeds
   `MAX_INPUT_TOKENS`, apply the single drop-and-recount step from §5.4.1.
3. **Price the worst case:** `input_tokens × input_rate + max_output_tokens × output_rate` (uncached input
   rate; `max_output_tokens` bounds reasoning tokens too), using the verified entry for the configured tier.
4. **Reserve** in one fenced transaction (§5.5) against **both** the project account (`PROJECT_BUDGET_USD`,
   default 20) and the conversation cap (`CONVERSATION_BUDGET_USD`, default 0.10) using conditional
   `UPDATE … WHERE spent + reserved + uncertain + :amount <= cap RETURNING`. If either fails, no request
   is sent.
5. **Send** generation with timeout `min(20 s, remaining deadline)`.
6. **Classify** the outcome. Charge certainty and retry eligibility are decided separately:

| Outcome | Reservation | Live continuation in this conversation | Retry |
|---|---|---|---|
| Response with usage (`completed`, or `incomplete` with usage) and a priced `service_tier` | Settle: priced usage → `spent`; release the remainder | Allowed (`incomplete` ends the turn with the limit message) | No |
| Response with usage but a `service_tier` not priced in `pricing.json` | `uncertain`, full amount kept | Blocked | No |
| Connection failed before the request was sent (DNS failure, TCP connect refused/timeout) | `released` | Allowed | Once, with a fresh reservation |
| 429, transient rate limit (`error.type == "rate_limit_error"` or `error.code ∈ {rate_limit_exceeded, slow_down}`) | `uncertain`, full amount kept | Allowed | Once, with a fresh reservation, only if `Retry-After` (default 1 s when absent) plus a 5 s minimum attempt fits in the remaining deadline. Otherwise the turn ends with the upstream-unavailable message |
| 429, quota/billing (`error.type == "insufficient_quota"`, or `error.code ∈ {credit_balance_exhausted, organization_spend_limit_exceeded, project_spend_limit_exceeded, organization_usage_limit_exceeded}`), or **any unclassified 429** | `uncertain`, full amount kept | Blocked (`provider_quota`) | No |
| 400, 401, 403, other 4xx | `uncertain`, full amount kept | Blocked (`provider_rejected`): the unchanged request or configuration would fail again | No |
| 5xx, client timeout, interrupted read, malformed response | `uncertain`, full amount kept | Blocked (`provider_outcome_unknown`) | No |

  The spec makes **no zero-charge claim for any HTTP error response**. OpenAI's error-code guide does not
  state whether failed requests are billed, so every provider HTTP error keeps its reservation as `uncertain`.
  `uncertain` amounts are never released automatically. They count against both caps until an operator
  reconciles them against the provider's usage dashboard (documented manual step). Only a connection failure
  before send, where no request left the client, releases a reservation.

- Ledger rows (`llm_calls`) record kind (`generation` | `token_count`), model, service tier, attempt, status
  (`reserved/settled/released/uncertain`), outcome class, reserved and estimated amounts, token counts,
  latency, provider request id. All values are **application estimates**, not a billing cap.
- Docs explain provider-side controls separately: a dedicated OpenAI project with a **hard monthly** project
  spend limit (requests return 429 `project_spend_limit_exceeded` when reached; enforcement can lag slightly)
  plus an **alert** threshold (notification only). Neither enforces a cumulative multi-month budget; the app
  ledger approximates that.

### 5.7 Observability

- JSON logs to stdout. Logging uses an **allowlist of metadata fields**, never payloads:
  `event, correlation_id, conversation_id, turn_id, claim_version, tool_name, status, error_code, duration_ms,
  http_status, attempt, model, service_tier, input_tokens, output_tokens, estimated_cost_usd, record_count`.
- Never logged and never shown in the debug drawer: raw user/assistant messages, tool arguments or results,
  addresses, names, CPFs, account numbers, knowledge answers, API keys, session tokens.
- A redaction filter (CPF, account-number, date, key patterns) remains as defense-in-depth only.
- `X-Correlation-ID` is generated per request (or accepted if well-formed), returned in the response, and
  propagated to mock-billing.
- **Documented limitation:** the database stores raw synthetic message text and tool arguments/results
  (`conversation_items`, needed for history replay), and the LLM sees the knowledge answers the customer types.

### 5.8 PT-BR message catalog (deterministic, not model-generated)

Generic auth failure (also used for lockout and `verification_busy`), verification expired, upstream
unavailable/timeout, limit reached, budget exhausted / live mode blocked, turn in progress, turn interrupted.
Kept in one module for review.

### 5.9 API (support-api)

| Method & path | Purpose |
|---|---|
| `POST /api/conversations` | Create conversation bound to the browser session |
| `POST /api/conversations/{id}/messages` | Submit a turn (§5.5); returns reply, turn status (`completed`/`interrupted`/…) + turn trace metadata |
| `GET /api/conversations/{id}` | Transcript + per-turn trace metadata (owned conversations only) |
| `GET /api/demo/customers` | Synthetic personas and their knowledge-check answers (labeled demo data; disabled when `DEMO_DATA_ENDPOINT_ENABLED=false`) |
| `GET /api/health` | Liveness + mode (`scripted`/`openai`, live enabled flag) |

`GET /api/demo/customers` reads the persona file `data/synthetic/personas.json` (the same file mock-billing
seeds from), never the `billing` schema.

### 5.10 Persistence (schema `support`)

`browser_sessions`, `conversations` (owner, `active_turn_id`, `claim_version`, `active_turn_lease_expires_at`,
turn_count, live_blocked_reason), `conversation_auth` (assurance, customer_ref, verified_at,
verified_expires_at, failed_attempts, window_started_at, locked_until), `identifier_lockouts` (hash,
failed_count, window_started_at, locked_until), `verification_attempts` (identifier hash, conversation, turn,
status `in_flight/failed/verified/upstream_error/abandoned`, expires_at), `turns` (client_message_id, text
hash, status `in_progress/completed/interrupted/failed`, correlation_id), `conversation_items`, `tool_calls`
(name, status, error_code, attempts, duration, record_count), `budget_accounts`, `conversation_budgets`,
`llm_calls`.

## 6. Web UI

- PT-BR chat panel with the offline-mode badge when applicable.
- English debug drawer (developer-facing): correlation id, per-turn tool calls (name, status, error code,
  attempts, duration, record count), model calls (kind, model, tier, tokens, estimated cost, outcome class).
  No payloads.
- *"Dados de demonstração"* panel listing personas and their knowledge-check answers, clearly labeled as
  synthetic.
- Client generates `client_message_id` per submission and reuses it on retry of a request whose response was
  lost. After an `interrupted` reply, the retry button uses a new id (§5.5).

## 7. Testing and acceptance criteria

Unit tests (pytest) for executor, identity/lockout, billing client retries and deadlines, loop limits,
history assembly, budget reservation math, outcome classification, message catalog. Integration tests against
Compose Postgres (separate test databases) and a live mock-billing process. Playwright tests in `web/`.

Milestone 1 is accepted when all of these pass:

1. Fresh Codespace → README steps → `docker compose up --build` brings up all four services.
2. Offline journey (no OpenAI key): ask about bills → asked to verify → correct answers → correct PT-BR bill summary. (Playwright)
3. Another customer's bill cannot be retrieved: executor-level (no identifier accepted; foreign `bill_id` → `not_found`) and billing-API-level tests.
4. Conversation ownership: a different browser session gets 404 for an existing `conversation_id` on every conversation endpoint.
5. Three wrong attempts lock the conversation; identical generic messages throughout. (Playwright + API)
6. Lockout persists across new conversations and new browser sessions for the same normalized identifier (formatting variants of the same CPF share the lock).
7. Upstream timeouts/5xx during verification are not counted as failed attempts and release their slots.
8. **Concurrent lockout admission:** 10 concurrent verification attempts for one identifier from 10 browser sessions, with mock billing delaying 500 ms and returning `{verified: false}` → at most 3 verify requests reach mock billing, the identifier ends locked, and every response carries the generic message. The same setup with mock billing returning 503 → `failed_count` stays 0 and all slots end `upstream_error`.
9. Billing timeout, 500, and outage each produce a recoverable PT-BR message with no fabricated data; trace shows the error code; total deadline respected.
10. `support_app` receives *permission denied* on schema `billing`.
11. Duplicate submissions: sequential replay returns the stored reply with no new model/tool calls; concurrent identical submissions produce exactly one turn; same id with different text → 422; concurrent different messages → one 409.
12. **Crash after send:** a worker reserves, the mocked provider receives the generation request, and the worker is killed before recording the outcome. After lease expiry, a duplicate submission returns the stored `interrupted` reply (not 409), the `llm_calls` row is `uncertain` with its full reservation, live generation is blocked for the conversation, and a new message is answered in scripted mode or with the live-blocked message. The startup sweep produces the same state without any request.
13. **Stale worker:** worker A claims a turn and is paused at any point before a pre-send gate (§5.5): after a model response, after a budget reservation, after verification-slot admission, or after a claim check with the local deadline then expired. The lease expires; worker B recovers and claims. When A resumes, all of A's fenced writes fail with `ClaimLost`, A sends no further provider or billing requests, and the transcript contains only B's items. The residual race in §5.5 (a process stall of ≥ 15 s after the gate passes) is documented and out of scope for this criterion.
14. **Late verification outcome after takeover:** a conversation has two failed attempts. Its third verification is in flight (mock billing delayed) when the worker loses its claim and recovery runs. The late `{verified: false}` marks the attempt `failed` and locks both the identifier and the conversation. Assurance, transcript and turn status are unchanged. Replaying the same outcome changes no counter. In the same setup, a late `{verified: true}` records the attempt as `verified` and leaves the conversation's assurance at `none`.
15. Concurrent budget reservations never exceed the project or conversation cap.
16. **Outcome classification:** each row of the §5.6 table, driven by mocked HTTP, produces the stated reservation status, live-block state and retry behavior. Specifically: no retry on 400/401/403 or quota 429; a transient 429 with `Retry-After` beyond the remaining deadline is not retried; a transient 429 within the deadline is retried once with a new reservation; unclassified 429 → no retry.
17. **Token counting:** the count and generation requests share `model`, `instructions`, `input`, `tools` and `tool_choice`. A count failure sends no generation request. A count above `MAX_INPUT_TOKENS` triggers exactly one drop-and-recount. Count time is charged to the turn deadline.
18. **History truncation (mocked provider):** with history spanning the truncation boundary (including a turn with two tool exchanges and reasoning items, and an interrupted turn with a dangling call), every request has no orphaned `function_call_output`, no `function_call` without an output, only whole turns dropped (oldest first, reasoning items with them), and no tool items from the interrupted turn. Each `reasoning_items` setting produces the replay described in §5.4.1. A current turn that alone exceeds the limit ends with the limit message and sends no request.
19. With live mode disabled, or any live-gate check failing (unverified pricing, billed or unverified token counting, missing model reasoning entry), zero requests reach the OpenAI API, including token counts (HTTP mock asserts no calls; client not constructed).
20. OpenAI path implemented and tested against mocked HTTP (tool-call round trip, usage reconciliation, retry rules). **Not executed live until spending is approved** — the milestone report will state this.
21. `/_dev/faults` returns 401 without a valid `X-Fault-Admin-Key`, rejects the billing service API key, and is not mounted when `FAULT_INJECTION_ENABLED=false`.
22. Captured logs contain correlation IDs and none of the forbidden fields/values from §5.7 (test asserts against seeded persona values).

## 8. Open items

**Blocking for enabling live generation** (each is a live-gate check in §5.6; not blocking for Milestone 1
acceptance, which runs on mocks):

- ~~Verify pricing for the selected model and processing tier.~~ Rev 5: `gpt-6-luna` default and flex
  rates recorded from the official pricing page on 2026-10-23.
- ~~Confirm that `POST /v1/responses/input_tokens` is unbilled.~~ Rev 5: still undocumented (checked
  2026-10-23), so counts are treated as billed and reserved (§5.6 gate item 3). Its rate limits remain
  undocumented.
- ~~Confirm the selected model's cross-turn reasoning behavior.~~ Rev 5: the reasoning guide lists only
  GPT-5.6 models and GPT-6.1 Sol as supporting `all_turns`; `gpt-6-luna` is recorded as `current_turn`
  (2026-10-23). The first live calls should confirm it with the count endpoint.

**Non-blocking:**

- Confirm `gpt-6-luna` structured-output/strict-schema support before relying on strict function schemas.
- Verify Python 3.12 wheel availability for all chosen dependencies at install time.
- Project 2 technical handoff (Milestone 4).

## 9. Revision history

- **Rev 2 (2026-10-22), review responses:**
  - The byte-based input bound is replaced by the provider token-count endpoint, behind the live gate and
    inside the turn deadline, failing closed. Live mode now requires verified pricing for the model and tier.
  - Claim versions now fence every state write; recovery persists `interrupted` turns and keeps possibly-sent
    reservations as `uncertain` with live generation blocked.
  - Lockout admission now reserves attempt slots atomically per identifier.
  - Charge certainty is separated from retry eligibility, and no HTTP error is treated as zero-cost.
  - History is assembled from complete turns and tool exchanges.
  - `/_dev/faults` now requires authentication.
  - CPF wording changed to "generated … synthetic fixture".
  - Allowed-origin configuration is documented for localhost and Codespaces.
- **Rev 3 (2026-10-22), review responses:**
  - Verification outcomes are recorded by an idempotent, unfenced attempt-outcome transaction keyed by
    attempt ID. It updates both the identifier and the conversation failure counters after takeover, never
    assurance or transcript. Conversation admission now counts in-flight slots too.
  - Live mode requires verified **unbilled** token counting; billed counting is unsupported in Milestone 1.
  - The live gate now covers every §8 blocking prerequisite, including per-model reasoning handling.
  - `conversation_items` now stores encrypted reasoning items.
  - Rev 3 wording fix: §5.4.1 now separates stored-item retention from per-request filtering of reasoning
    items.
- **Rev 4 (2026-10-22), plan review:**
  - §5.5 adds the pre-send gate (claim re-check plus a local deadline check, with total-time-capped requests)
    and documents the residual stall race that fencing can't close without receiver-side token checks.
  - AC 13 is scoped to pauses before a pre-send gate; the residual race is excluded from it explicitly.
- **Rev 5 (2026-10-23), opening the live gate:**
  - §5.6 gate item 3 now accepts billed token counting, with a reservation path for count requests (reserve,
    pre-send gate, settle, release or uncertain, and blocking for deterministic failures).
  - §8 blocking items resolved: verified `gpt-6-luna` pricing and reasoning replay; counting treated as billed.
  - `PROJECT_BUDGET_USD` (Settings) is the single source of the project cap: startup writes it to
    `budget_accounts`. Migration 0003 adds non-negative CHECK constraints to every money and token column.
  - `OPENAI_REASONING_EFFORT` (default `low`) is sent on count and generation requests.
  - Live mode still needs `LLM_PROVIDER=openai`, `LIVE_LLM_ENABLED=true` and a key, all set by the owner.
