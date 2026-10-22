# Milestone 1 — Chat Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Compose stack in Codespaces where a customer chats in PT-BR, verifies with knowledge checks only when
customer data is needed, and gets an accurate bill summary, entirely offline (scripted model). The OpenAI path is
built and tested against mocked HTTP only.

**Architecture:** Two FastAPI services share one Postgres 16 instance, each with its own schema and role:
`mock-billing` (system of record) and `support-api` (agent core, identity, turns, budget). A Vite/React UI reaches
`support-api` through a same-origin `/api` proxy. Every customer-data path goes through the tool executor.
Concurrency safety comes from conditional SQL updates plus fenced turn claims.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 Core (async) + psycopg 3, Alembic, Pydantic v2, httpx, OpenAI
Python SDK (`max_retries=0`), pytest + pytest-asyncio + respx; Vite + React 18 (JavaScript) + Playwright; Docker
Compose.

**Spec:** [docs/superpowers/specs/2026-10-22-milestone-1-chat-vertical-slice-design.md](../specs/2026-10-22-milestone-1-chat-vertical-slice-design.md)
(revision 3). Section references (§) below point there; acceptance criteria are cited as **AC n**.

## Global Constraints

- **No paid calls.** No task, script, fixture or doc step sets `LIVE_LLM_ENABLED=true` against the real API.
  `config/pricing.json` and `config/models.json` ship with `verified: false` placeholders, so the live gate (§5.6)
  stays closed. Every OpenAI test runs under respx with all routes mocked. An autouse fixture fails any test that
  attempts a real connection to `api.openai.com`.
- Python 3.12 for both services. Each service has its own `pyproject.toml`, Alembic migrations and DB role
  (`support_app` → schema `support`; `billing_app` → schema `billing`). `support_app` has **no** grants on `billing`.
- Customer-facing text is PT-BR, and all of it comes from `support_api/catalog.py` or the model. Code, logs and the
  debug drawer are English.
- Synthetic data only. CPFs are "generated CPF (valid check digits) used solely as a synthetic fixture".
- Logs use the allowlist in §5.7 verbatim. Payloads are never logged.
- Retry logic lives in exactly one place per integration: `BillingClient` and `OpenAIResponsesModel`. SDK retries
  are off (`max_retries=0`).
- Every timeout is `min(per_attempt, deadline.remaining())`.
- Limits (§5.4): model calls/turn 5; tool calls/turn 4; turns/conversation 30; `max_output_tokens` 800; turn
  deadline 45 s; LLM attempt 20 s; token count 10 s, no retry; billing attempt 3 s; billing tool total 8 s;
  billing GET retries ≤ 2 with backoff 200 ms / 600 ms + jitter; `HISTORY_MAX_TURNS` 6.
- Identity (§5.2): 3 failures / 15 min window → 15 min lock; slot TTL 30 s; verification valid 30 min.
- Turn lease 60 s (§5.5). Budget defaults: `PROJECT_BUDGET_USD=20`, `CONVERSATION_BUDGET_USD=0.10`.
- **Decided here (spec leaves open):** `MAX_INPUT_TOKENS=8000`; `OPENAI_SERVICE_TIER=default`; money is
  `NUMERIC(12,6)` / `Decimal`, reservations rounded up (`ROUND_CEILING`) to 6 places.
- Row-lock order: `conversations → verification_attempts → identifier_lockouts → conversation_auth` and
  `conversations → llm_calls → budget_accounts → conversation_budgets`. Transactions skip tables they don't touch.
- **Fencing (§5.5).** Every write by a turn worker runs inside `fenced()`. There are exactly two unfenced
  exceptions, both keyed by their own row: the attempt-outcome transaction (`record_outcome`) and usage
  settlement (`settle`). Recovery writes use the caller's transaction, which already holds the conversation row
  lock. The worker calls `assert_claim` (Task 8) immediately before every outbound request: token count,
  generation, each billing attempt, and each retry. A pause can't carry a stale worker straight into an HTTP
  send. The check-then-send gap is bounded by the 60 s lease; the conservative ledger (`uncertain`, never
  released) covers what remains.

## Review Focus

1. **Identifier and date formats typed by people.** `123.456.789-09`, `12345678909`, `123 456 789 09`, and
   `1/2/1980` vs `01/02/1980` must parse the same way. Pinned in Task 2 (`normalize`) and Task 15 (scripted parser).
2. **Empty results.** Persona B asking for "faturas em aberto" gets an explicit "Não encontrei faturas em aberto."
   answer, never invented bills. The empty-result wording follows the requested status (em aberto / em atraso /
   pagas / none). Pinned in Task 15.
3. **Verification expiring mid-conversation.** A tool call after `verified_expires_at` returns
   `authentication_required` and the verification-expired message, even within the same turn. Pinned in Task 10.
4. **2,000-character limit with accents and emoji.** Length counts Unicode code points, not bytes. 2,000 `ç`
   passes; 2,001 → 422. Pinned in Task 17.
5. **Codespaces origin.** A request with `Origin: https://<name>-5173.app.github.dev` passes only when
   `ALLOWED_ORIGINS` lists it exactly; a trailing slash or other port fails. Pinned in Task 6.

---

## Dependencies

### Task order

```
T1 scaffold ─┬─ T2 personas ── T3 billing API ── T4 faults
             └─ T5 support-api skeleton ─┬─ T6 sessions/ownership
                                         ├─ T7 billing client (integration tests need T3)
                                         ├─ T8 turn claims/fencing
                                         ├─ T11 live gate/pricing
                                         ├─ T13 outcome classification
                                         └─ T14 history assembly
T7 + T8 ── T9 identity ── T10 tools/executor
T8 + T11 ── T12 budget ledger
T10 + T14 ── T15 ChatModel + ScriptedModel ── T16 AgentCore loop
T16 + T9 + T12 ── T17 turn service + recovery   (AC 11, 12, 13, 14)
T17 + T11..T14 ── T18 OpenAIResponsesModel      (AC 16, 17, 18, 19, 20)
T17 ── T19 web UI ── T20 full stack, docs, acceptance run
```

Tasks on separate branches of the tree (for example T11–T14) can run in either order.

### Packages (pin exact versions in each `pyproject.toml` / `package.json` after first install)

| Service | Runtime | Dev |
|---|---|---|
| mock_billing | fastapi, uvicorn[standard], sqlalchemy[asyncio]>=2.0, psycopg[binary]>=3.2, alembic, pydantic>=2, pydantic-settings | pytest, pytest-asyncio, httpx, ruff |
| support_api | same as above + httpx, openai (version exposing `AsyncOpenAI().responses`) | pytest, pytest-asyncio, respx, ruff |
| web | react@18.3.1, react-dom@18.3.1 | vite@5, @vitejs/plugin-react, @playwright/test |

For the open item "verify Python 3.12 wheels", Task 1 Step 4 records the result.

### Environment

Docker with Compose v2 (docker-in-docker feature in the devcontainer), Python 3.12, Node 20. No OpenAI key.

---

## File Structure

```
.devcontainer/devcontainer.json        python:3.12 image, node 20, docker-in-docker, ports 5173/8000/8001
docker-compose.yml                     db, mock-billing, support-api, web
.env.example                           placeholders; OPENAI_API_KEY empty, LIVE_LLM_ENABLED=false
scripts/init-env.sh                    writes .env with random secrets + ALLOWED_ORIGINS (§5.1)
scripts/gen_personas.py                deterministic persona generator (seeded)
scripts/smoke.sh                       health checks for AC 1
db/init/01-init.sh                     roles, schemas, grants, uas + uas_test databases
data/synthetic/personas.json           5 personas (generated, committed)
services/mock_billing/
  pyproject.toml  alembic.ini  migrations/
  src/mock_billing/{app.py,settings.py,db.py,auth.py,models.py,seed.py,routes.py,faults.py,cpf.py}
  tests/
services/support_api/
  pyproject.toml  alembic.ini  migrations/
  config/pricing.json  config/models.json
  prompts/system_ptbr.md
  src/support_api/
    app.py settings.py db.py deadline.py catalog.py logging.py checkpoints.py
    channels/chat.py  sessions.py  demo.py
    identity/{normalize.py,attempts.py,assurance.py}
    integrations/billing/{client.py,schemas.py}
    tools/{schemas.py,executor.py}
    turns/{claim.py,recovery.py,service.py}
    usage/{pricing.py,budget.py,classify.py}
    agent/{core.py,history.py,format_ptbr.py}
    agent/models/{base.py,scripted.py,fake.py,openai_model.py}
  tests/{conftest.py,unit/,integration/}
web/  package.json vite.config.js index.html src/{main.jsx,App.jsx,api.js,ChatPanel.jsx,DebugDrawer.jsx,DemoDataPanel.jsx} tests/
docs/{troubleshooting.md,provider-controls.md}  README.md
```

---

### Task 1: Repository scaffold, devcontainer, database roles

**Files:**
- Create: `.devcontainer/devcontainer.json`, `docker-compose.yml` (db service only for now), `.env.example`,
  `scripts/init-env.sh`, `db/init/01-init.sh`, `services/mock_billing/pyproject.toml`,
  `services/support_api/pyproject.toml`, `.gitignore` (`.env`, `node_modules`, `.venv`, `__pycache__`)
- Test: `services/support_api/tests/integration/test_db_grants.py`

**Interfaces:**
- Produces: env vars `POSTGRES_PASSWORD`, `SUPPORT_DB_PASSWORD`, `BILLING_DB_PASSWORD`; databases `uas` and
  `uas_test`, each with schemas `support` (owner `support_app`) and `billing` (owner `billing_app`); connection
  URLs `SUPPORT_DATABASE_URL`, `BILLING_DATABASE_URL`, `SUPPORT_TEST_DATABASE_URL`, `BILLING_TEST_DATABASE_URL`
  (`postgresql+psycopg://…`).

- [ ] **Step 1: Write the failing grant test (AC 10)**

```python
async def test_support_app_denied_on_billing_schema(support_test_engine):
    async with support_test_engine.connect() as conn:
        with pytest.raises(sqlalchemy.exc.ProgrammingError, match="permission denied"):
            await conn.execute(text("CREATE TABLE billing.probe (id int)"))
    async with support_test_engine.connect() as conn:
        with pytest.raises(sqlalchemy.exc.ProgrammingError, match="permission denied"):
            await conn.execute(text("SELECT 1 FROM billing.customers LIMIT 1"))
```

`billing.customers` doesn't exist until Task 3. The second assertion must fail with permission denied on the
schema, not "does not exist": Postgres checks `USAGE` on the schema first. `support_test_engine` fixture lives in
`tests/conftest.py`, built from `SUPPORT_TEST_DATABASE_URL`.

- [ ] **Step 2: Run it — expect connection failure**

Run: `cd services/support_api && pytest tests/integration/test_db_grants.py -v`
Expected: FAIL/ERROR (no database).

- [ ] **Step 3: Implement scaffold**

`db/init/01-init.sh` (runs as superuser via the Postgres entrypoint) creates roles `support_app` and `billing_app`
with `LOGIN` and passwords from env. It creates databases `uas` and `uas_test`. In each database it creates both
schemas owned by their role and runs `REVOKE ALL ON SCHEMA billing FROM PUBLIC` and `REVOKE ALL ON SCHEMA support
FROM PUBLIC`. `scripts/init-env.sh` generates secrets with `openssl rand -hex 32` for the DB passwords,
`BILLING_API_KEY`, `FAULT_ADMIN_KEY` and `IDENTIFIER_HASH_KEY`. It sets `ALLOWED_ORIGINS` per §5.1 (Codespaces
origin when `CODESPACE_NAME` is set, else `http://localhost:5173`), `LLM_PROVIDER=scripted`,
`LIVE_LLM_ENABLED=false`, and leaves `OPENAI_API_KEY=` empty. It refuses to overwrite an existing `.env`.

- [ ] **Step 4: Bring up the database, install both services, run the test**

Run: `scripts/init-env.sh && docker compose up -d db && pip install -e 'services/mock_billing[dev]' -e 'services/support_api[dev]' && (cd services/support_api && pytest tests/integration/test_db_grants.py -v)`
Expected: PASS. Every dependency installs from wheels on 3.12 (`pip install --only-binary=:all:` succeeds).
Record the outcome in the commit message; it resolves the spec open item.

- [ ] **Step 5: Commit** — `git commit -m "chore: scaffold repo, devcontainer, db roles and grants"`

---

### Task 2: Synthetic personas and CPF utilities

**Files:**
- Create: `services/mock_billing/src/mock_billing/cpf.py`, `scripts/gen_personas.py`, `data/synthetic/personas.json`
- Test: `services/mock_billing/tests/test_personas.py`

**Interfaces:**
- Produces: `cpf.check_digits(base9: str) -> str`, `cpf.is_valid(digits11: str) -> bool`.
  `personas.json` schema: `[{persona: "A".."E", customer_ref: "cus_…", first_name, full_name, cpf: "11 digits",
  account_number: "10 digits", birth_date: "YYYY-MM-DD", phone: "+55…", situation, address: {street, neighborhood,
  city, state}, tariff_class, account_status, installations: [{installation_id, label}], bills: [{bill_id,
  installation_id, reference_month: "YYYY-MM", issue_date, due_date, amount_brl: "123.45", status, consumption_kwh,
  tariff_flag: "verde"|"amarela"|"vermelha", line_items: [{description, amount_brl}]}]}]`.

- [ ] **Step 1: Write failing tests**

```python
def test_five_personas_with_required_situations(personas):
    assert [p["persona"] for p in personas] == ["A", "B", "C", "D", "E"]
    a, b, c, d, _ = personas
    assert sorted(x["status"] for x in a["bills"][:2]) == ["open", "overdue"]
    assert all(x["status"] == "paid" for x in b["bills"])
    assert len(c["installations"]) == 2
    assert d["bills"][0]["consumption_kwh"] >= 1.8 * statistics.mean(x["consumption_kwh"] for x in d["bills"][1:])

def test_cpfs_have_valid_check_digits_and_unique_identifiers(personas):
    assert all(cpf.is_valid(p["cpf"]) for p in personas)
    assert len({p["cpf"] for p in personas}) == 5 and all(len(p["account_number"]) == 10 for p in personas)

def test_bills_shape(personas):
    for p in personas:
        assert 6 <= len(p["bills"]) <= 12
        for bill in p["bills"]:
            assert Decimal(bill["amount_brl"]) == sum(Decimal(li["amount_brl"]) for li in bill["line_items"])

def test_generator_is_deterministic(tmp_path):
    # running scripts/gen_personas.py --seed 20261022 reproduces the committed file byte-for-byte
```

- [ ] **Step 2: Run — expect FAIL** (`pytest services/mock_billing/tests/test_personas.py -v`)
- [ ] **Step 3: Implement** `cpf.py`, and `gen_personas.py --seed 20261022 --out data/synthetic/personas.json`
  using `random.Random(seed)`. Names must be obviously fictional (e.g. "Ana Exemplo da Silva"); amounts are
  `Decimal` strings. Generate and commit the file.
- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Commit** — `feat(data): add seeded synthetic personas and CPF check digits`

---

### Task 3: Mock billing service API

**Files:**
- Create: `services/mock_billing/src/mock_billing/{settings.py,db.py,models.py,seed.py,auth.py,routes.py,app.py}`,
  `services/mock_billing/migrations/versions/0001_billing.py`, `alembic.ini`
- Modify: `docker-compose.yml` (add `mock-billing` on :8001; runs `alembic upgrade head`, seeds, then uvicorn)
- Test: `services/mock_billing/tests/test_api.py`, `tests/conftest.py`

**Interfaces:**
- Produces: `create_app(settings: Settings) -> FastAPI`. HTTP contract exactly as §4. Response models:
  `VerifyResponse{verified: bool, customer_ref?: str, first_name?: str}`;
  `Account{customer_ref, masked_cpf: "***.***.***-NN", installations, address, tariff_class, account_status}`;
  `BillSummary{bill_id, installation_id, reference_month, due_date, amount_brl: str, status, consumption_kwh,
  tariff_flag}`; `BillDetail = BillSummary + {issue_date, line_items}`. `/v1/identity/verify` compares the
  identifier against CPF (11 digits) or account number (10 digits) after stripping non-digits.
- Settings: `BILLING_DATABASE_URL`, `BILLING_API_KEY`, `FAULT_INJECTION_ENABLED=false`, `FAULT_ADMIN_KEY`.

- [ ] **Step 1: Write failing tests** (seeded test DB, `httpx.AsyncClient(transport=ASGITransport(app))`)

```python
async def test_v1_requires_api_key(client):
    assert (await client.get(f"/v1/customers/{A.ref}/account")).status_code == 401
    assert (await client.get(f"/v1/customers/{A.ref}/account", headers={"X-API-Key": "wrong"})).status_code == 401

async def test_verify_unknown_and_wrong_answers_identical(client, auth):
    unknown = await client.post("/v1/identity/verify", headers=auth, json={"identifier": "00000000000", "birth_date": A.birth, "phone_last4": A.last4})
    wrong = await client.post("/v1/identity/verify", headers=auth, json={"identifier": A.cpf, "birth_date": "1900-01-01", "phone_last4": A.last4})
    assert unknown.status_code == wrong.status_code == 200 and unknown.json() == wrong.json() == {"verified": False}

async def test_verify_success_by_cpf_and_account(client, auth): ...  # both → {"verified": True, "customer_ref": A.ref, "first_name": A.first}

async def test_foreign_bill_is_404_like_nonexistent(client, auth):
    foreign = await client.get(f"/v1/customers/{A.ref}/bills/{B.bills[0]}", headers=auth)
    missing = await client.get(f"/v1/customers/{A.ref}/bills/bill_does_not_exist", headers=auth)
    assert foreign.status_code == missing.status_code == 404 and foreign.json() == missing.json()

async def test_list_bills_filter_order_limit(client, auth):
    # status=overdue filters; newest first by reference_month; limit=13 → 422; default limit 12

async def test_account_masks_cpf(client, auth):
    assert re.fullmatch(r"\*\*\*\.\*\*\*\.\*\*\*-\d\d", body["masked_cpf"])

async def test_health_needs_no_auth(client): ...
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement.** Use `hmac.compare_digest` for the key. Accept `X-Correlation-ID` and log it. Seed is
  idempotent (upsert by `customer_ref`) from `data/synthetic/personas.json`.
- [ ] **Step 4: Run — expect PASS** (`cd services/mock_billing && pytest -v`)
- [ ] **Step 5: Commit** — `feat(billing): mock billing API with seeded personas`

---

### Task 4: Fault injection (authenticated)

**Files:**
- Create: `services/mock_billing/src/mock_billing/faults.py`
- Modify: `app.py` (mount only when enabled; middleware applies rules)
- Test: `services/mock_billing/tests/test_faults.py`

**Interfaces:**
- Produces: `PUT /_dev/faults` body `{rules: [{route_name: "verify"|"account"|"list_bills"|"bill_detail",
  customer_ref?: str, effect: "timeout"|"http_500"|"unavailable"|"delay_ms:N"}]}` replaces all rules;
  `DELETE /_dev/faults` clears them. Header `X-Fault-Admin-Key`. `timeout` sleeps 30 s (longer than any client
  timeout).

- [ ] **Step 1: Write failing tests (AC 21)**

```python
async def test_faults_require_admin_key(client_enabled, settings):
    assert (await client_enabled.put("/_dev/faults", json={"rules": []})).status_code == 401
    assert (await client_enabled.put("/_dev/faults", json={"rules": []}, headers={"X-Fault-Admin-Key": settings.billing_api_key})).status_code == 401
    assert (await client_enabled.put("/_dev/faults", json={"rules": []}, headers={"X-Fault-Admin-Key": settings.fault_admin_key})).status_code == 204

async def test_faults_not_mounted_when_disabled(client_disabled, settings):
    r = await client_disabled.put("/_dev/faults", json={"rules": []}, headers={"X-Fault-Admin-Key": settings.fault_admin_key})
    assert r.status_code == 404

async def test_rule_scoped_to_customer(client_enabled, admin, auth):
    # http_500 for (account, E.ref): E → 500, A → 200; "unavailable" → 503; delay_ms:200 → ≥200 ms; DELETE restores
```

- [ ] **Step 2–4:** run (FAIL) → implement → run (PASS)
- [ ] **Step 5: Commit** — `feat(billing): authenticated deterministic fault injection`

---

### Task 5: support-api skeleton, schema, catalog, health, demo data

**Files:**
- Create: `services/support_api/src/support_api/{settings.py,db.py,app.py,catalog.py,deadline.py,checkpoints.py,demo.py}`,
  `migrations/versions/0001_support.py`, `config/pricing.json`, `config/models.json`, `tests/conftest.py`
- Modify: `docker-compose.yml` (add `support-api` on :8000)
- Test: `tests/unit/test_catalog.py`, `tests/unit/test_deadline.py`, `tests/integration/test_health_demo.py`

**Interfaces:**
- Produces:
  - `Settings` (pydantic-settings) with every env var named in this plan; defaults from Global Constraints.
  - `Database(url)` with `begin() -> AsyncContextManager[AsyncConnection]` and `engine`.
  - `create_app(settings, *, model: ChatModel | None = None, billing_transport=None, checkpoints=None) -> FastAPI`
    (later tasks add routers).
  - `Deadline(seconds: float, clock=time.monotonic)` with `.remaining() -> float`,
    `.timeout(per_attempt: float) -> float` (= `max(0, min(per_attempt, remaining))`), `.expired -> bool`.
  - `Checkpoints` with `async hit(name: str) -> None` (no-op default; tests subclass to pause or crash).
    `SimulatedCrash(BaseException)`.
  - `catalog.MESSAGES: dict[str, str]` and `catalog.message(key) -> str`.
- Schema (one migration, all §5.10 tables, schema `support`):
  - `browser_sessions(id uuid pk, token_sha256 bytea unique, created_at, expires_at)`
  - `conversations(id uuid pk, browser_session_id fk, created_at, turn_count int 0, active_turn_id uuid null,
    claim_version bigint 0, active_turn_lease_expires_at timestamptz null, live_blocked_reason text null)`
  - `conversation_auth(conversation_id pk fk, assurance_level text check in (none,kba,step_up) default none,
    customer_ref, first_name, verified_at, verified_expires_at, failed_attempts int 0, window_started_at,
    locked_until)`
  - `identifier_lockouts(identifier_hash text pk, failed_count int 0, window_started_at, locked_until)`
  - `verification_attempts(id uuid pk, identifier_hash, conversation_id, turn_id, status check in
    (in_flight,failed,verified,upstream_error,abandoned), created_at, expires_at)`, indexed on
    `(identifier_hash, status, expires_at)` and `(conversation_id, status, expires_at)`
  - `turns(id uuid pk, conversation_id, client_message_id uuid, text_sha256 text, status check in
    (in_progress,completed,interrupted,failed), reply text null, correlation_id, created_at, finished_at)`,
    `UNIQUE(conversation_id, client_message_id)`
  - `conversation_items(id bigserial, conversation_id, turn_id, seq int, kind check in (user_message,
    assistant_message,function_call,function_call_output,reasoning), call_id text null, model text null,
    payload jsonb)`, `UNIQUE(turn_id, seq)`
  - `tool_calls(id uuid, turn_id, name, status, error_code, attempts, duration_ms, record_count, created_at)`
  - `budget_accounts(id text pk, cap_usd, spent_usd, reserved_usd, uncertain_usd)`, with row `'project'` seeded by
    the migration from `PROJECT_BUDGET_USD`
  - `conversation_budgets(conversation_id pk, cap_usd, spent_usd, reserved_usd, uncertain_usd)`
  - `llm_calls(id uuid pk, conversation_id, turn_id, kind check in (generation,token_count), model, service_tier,
    attempt, status check in (reserved,settled,released,uncertain), outcome_class, reserved_usd, estimated_usd,
    input_tokens, output_tokens, latency_ms, provider_request_id, created_at)`
- Catalog copy (exact):

```python
MESSAGES = {
    "auth_failed": "Não foi possível confirmar seus dados. Verifique as informações e tente novamente mais tarde.",
    "verification_expired": "Por segurança, sua verificação expirou. Para continuar, confirme seus dados novamente.",
    "upstream_unavailable": "Não foi possível concluir sua solicitação no momento. Tente novamente em alguns minutos.",
    "limit_reached": "Não consegui concluir sua solicitação agora. Tente novamente em instantes.",
    "live_blocked": "As respostas automáticas estão indisponíveis nesta conversa no momento. Tente novamente mais tarde.",
    "turn_in_progress": "Ainda estou processando sua mensagem anterior. Aguarde um momento.",
    "turn_interrupted": "Não foi possível concluir sua mensagem. Envie novamente para tentar outra vez.",
    "offline_badge": "Modo offline — respostas simuladas",
}
```

- `config/pricing.json` placeholder (gate closed):
  `{"models": {"gpt-6-luna": {"default": {"input_per_mtok": null, "cached_input_per_mtok": null,
  "output_per_mtok": null, "source_url": null, "verified_on": null, "verified": false}}},
  "token_count": {"billed": null, "source_url": null, "verified_on": null}}`
- `config/models.json` placeholder: `{"gpt-6-luna": {"reasoning_items": null, "source_url": null,
  "verified_on": null, "verified": false}}`

- [ ] **Step 1: Write failing tests**

```python
def test_catalog_has_all_keys():
    assert set(MESSAGES) >= {"auth_failed", "verification_expired", "upstream_unavailable", "limit_reached",
                             "live_blocked", "turn_in_progress", "turn_interrupted", "offline_badge"}

def test_deadline_caps_timeout(fake_clock):
    d = Deadline(45, clock=fake_clock); fake_clock.advance(40)
    assert d.timeout(20) == 5 and not d.expired; fake_clock.advance(6); assert d.timeout(20) == 0 and d.expired

async def test_health_reports_scripted_mode(client):
    assert (await client.get("/api/health")).json() == {"status": "ok", "mode": "scripted", "live_enabled": False}

async def test_demo_customers_reads_persona_file(client):  # 5 entries with cpf, account_number, birth_date, phone_last4, "synthetic": True
async def test_demo_customers_disabled(client_demo_off):   # 404
async def test_migration_seeds_project_budget(support_db):  # budget_accounts 'project' cap 20
async def test_network_guard_blocks_openai():               # autouse guard is active from this task on
    with pytest.raises(RealNetworkBlocked):
        async with httpx.AsyncClient() as c: await c.get("https://api.openai.com/v1/models")
```

The autouse network guard goes into `tests/conftest.py` **in this task**, before any provider-related test
exists. It patches `httpx.AsyncHTTPTransport.handle_async_request` and `socket.create_connection` to raise
`RealNetworkBlocked` for `api.openai.com`. respx-mocked routes never reach the real transport, so they still
work.

- [ ] **Step 2–4:** FAIL → implement → PASS (`cd services/support_api && pytest -v`)
- [ ] **Step 5: Commit** — `feat(support): skeleton, schema, catalog, health and demo data`

---

### Task 6: Browser sessions, ownership, CSRF checks, logging

**Files:**
- Create: `src/support_api/sessions.py`, `src/support_api/logging.py`, `src/support_api/channels/chat.py`
  (conversation create and read only)
- Test: `tests/integration/test_sessions_ownership.py`, `tests/unit/test_logging.py`

**Interfaces:**
- Produces:
  - FastAPI dependency `current_session(request, response) -> BrowserSession` sets cookie
    `uas_session=<token>; HttpOnly; Secure; SameSite=Lax; Path=/api`, stores `sha256(token)`, expiry 8 h absolute.
  - Dependency `require_json_and_origin` → 415 `unsupported_media_type`, or 403 `{"error": "origin_not_allowed"}`.
  - `load_owned_conversation(conn, conversation_id, session_id) -> Row` raises `NotFound` (→ 404
    `{"error": "not_found"}`).
  - `POST /api/conversations` → `201 {"conversation_id"}`. `GET /api/conversations/{id}` → `{"turns": [...]}`
    (filled in Task 17).
  - `logging.configure()` installs a JSON formatter that emits only allowlisted keys (§5.7 list plus `ts`, `level`,
    `logger`) and drops everything else. `RedactionFilter` masks CPF/account/date/key patterns in `event`. A
    middleware handles `X-Correlation-ID`: it accepts the header if it matches `^[A-Za-z0-9-]{8,64}$`, otherwise
    generates a uuid4, echoes it in the response, and stores it in a contextvar `correlation_id`.

- [ ] **Step 1: Write failing tests**

```python
async def test_other_session_gets_404_on_every_conversation_endpoint(app):  # AC 4
    a, b = new_client(app), new_client(app)
    cid = (await a.post("/api/conversations", json={}, headers=ORIGIN)).json()["conversation_id"]
    assert (await b.get(f"/api/conversations/{cid}")).status_code == 404
    r = await b.post(f"/api/conversations/{cid}/messages", headers=ORIGIN, json={"client_message_id": str(uuid4()), "text": "oi"})
    assert r.status_code == 404 and r.json() == (await b.get(f"/api/conversations/{uuid4()}")).json()

async def test_cookie_attributes_and_hash_only(app, support_db): ...  # Set-Cookie flags; DB has no raw token

@pytest.mark.parametrize("origin,ok", [
    ("https://demo-5173.app.github.dev", True), ("https://demo-5173.app.github.dev/", False),
    ("https://demo-8000.app.github.dev", False), ("http://localhost:5173", False), (None, False)])
async def test_origin_exact_match(app_with_origins, origin, ok):  # ALLOWED_ORIGINS="https://demo-5173.app.github.dev"

async def test_non_json_rejected(app): ...  # 415

def test_log_allowlist_drops_payloads(caplog_json):
    log.info("tool_done", extra={"tool_name": "list_bills", "text": "CPF 123.456.789-09", "birth_date": "1980-02-01"})
    assert caplog_json[-1].keys() <= ALLOWED | {"ts", "level", "logger"} and "123.456" not in caplog_json.raw

async def test_correlation_id_generated_or_echoed(app): ...
```

- [ ] **Step 2–4:** FAIL → implement → PASS
- [ ] **Step 5: Commit** — `feat(support): browser sessions, ownership, origin checks, JSON logging`

---

### Task 7: Typed billing client

**Files:**
- Create: `src/support_api/integrations/billing/{schemas.py,client.py}`
- Test: `tests/unit/test_billing_client.py` (respx), `tests/integration/test_billing_client_live.py`
  (mock-billing subprocess fixture `billing_server` on a free port against `uas_test`)

**Interfaces:**
- Consumes: `Deadline` (Task 5); billing HTTP contract (Task 3).
- Produces:

```python
class BillingError(Exception):
    code: Literal["upstream_timeout", "upstream_unavailable", "not_found"]; attempts: int
Guard = Callable[[], Awaitable[None]]   # raises to abort before an attempt is sent
class BillingClient:
    def __init__(self, base_url: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 sleep=asyncio.sleep, rng=random.random): ...
    async def verify(self, identifier_digits: str, birth_date: date, phone_last4: str, *, deadline: Deadline, guard: Guard | None = None) -> VerifyResult
    async def get_account(self, ref: str, *, deadline: Deadline, guard: Guard | None = None) -> Account
    async def list_bills(self, ref: str, *, status: str | None, limit: int, deadline: Deadline, guard: Guard | None = None) -> list[BillSummary]
    async def get_bill(self, ref: str, bill_id: str, *, deadline: Deadline, guard: Guard | None = None) -> BillDetail
```

  `guard` is awaited immediately before every HTTP attempt, including the first attempt and each retry (after the
  backoff sleep). Any exception it raises propagates unchanged, so the client never wraps it in `BillingError`.
  The client stays claim-agnostic, and the executor (Task 10) passes `lambda: assert_claim(db, claim)`.

  Each call makes its own tool `Deadline(8)` capped by the turn deadline (`min` of both). The per-attempt timeout
  is 3 s. GETs retry ≤ 2 on timeout, 5xx or connection error, sleeping `base * (1 + 0.25 * rng())` with base
  0.2/0.6 s, but only if the sleep fits within the remaining deadline. `verify` (POST) is never retried. 404 →
  `not_found` (never retried). It sends `X-API-Key` and `X-Correlation-ID` from the contextvar, and returns
  `attempts` for the trace.

- [ ] **Step 1: Write failing tests**

```python
async def test_get_retries_twice_then_unavailable(respx_mock, fake_sleep):
    route = respx_mock.get(ACCOUNT_URL).mock(side_effect=[httpx.Response(500)] * 3)
    with pytest.raises(BillingError) as e: await client.get_account("cus_a", deadline=Deadline(45))
    assert e.value.code == "upstream_unavailable" and route.call_count == 3 and e.value.attempts == 3
    assert [round(s, 1) for s in fake_sleep.calls] in ([0.2, 0.6], [0.2, 0.7], [0.3, 0.6], [0.3, 0.7])  # jitter ≤ 25 %

async def test_verify_post_never_retried(respx_mock):  # 503 once → upstream_unavailable, call_count == 1
async def test_timeout_capped_by_turn_deadline(respx_mock):  # Deadline(1) → httpx timeout ≤ 1 s
async def test_tool_total_deadline_8s(respx_mock, fake_clock): ...  # three 3 s timeouts cannot exceed 8 s; stops early
async def test_404_maps_to_not_found_without_retry(respx_mock): ...
async def test_guard_runs_before_every_attempt(respx_mock, fake_sleep):
    route = respx_mock.get(ACCOUNT_URL).mock(side_effect=[httpx.Response(500)] * 3)
    guard = AsyncMock(side_effect=[None, ClaimLostStub()])        # second check fails
    with pytest.raises(ClaimLostStub): await client.get_account("cus_a", deadline=Deadline(45), guard=guard)
    assert route.call_count == 1 and guard.await_count == 2       # retry never sent
async def test_sends_api_key_and_correlation_id(respx_mock): ...
async def test_live_round_trip_against_mock_billing(billing_server): ...  # integration
```

- [ ] **Step 2–4:** FAIL → implement → PASS
- [ ] **Step 5: Commit** — `feat(support): typed billing client with bounded retries`

---

### Task 8: Turn claims and fencing

**Files:**
- Create: `src/support_api/turns/claim.py`
- Test: `tests/integration/test_claim.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Claim: conversation_id: UUID; turn_id: UUID; claim_version: int
class ClaimLost(Exception): ...
async def try_claim(conn: AsyncConnection, conversation_id: UUID, turn_id: UUID, lease_s: int = 60) -> Claim | None
@asynccontextmanager
async def fenced(db: Database, claim: Claim) -> AsyncIterator[AsyncConnection]  # raises ClaimLost; rolls back
async def release(conn: AsyncConnection, claim: Claim) -> None  # call inside fenced(); clears active_turn_*
async def assert_claim(db: Database, claim: Claim) -> None       # fenced no-op; raises ClaimLost
```

`try_claim` runs exactly the §5.5 conditional `UPDATE … RETURNING claim_version`. `fenced` opens a transaction and
runs `SELECT 1 FROM support.conversations WHERE id=:c AND active_turn_id=:t AND claim_version=:v FOR UPDATE`.
Zero rows → rollback and `ClaimLost`. `assert_claim` is `async with fenced(db, claim): pass`. Workers call it
right before every outbound request (Global Constraints, Fencing).

- [ ] **Step 1: Write failing tests**

```python
async def test_concurrent_claims_one_winner(db, conversation):
    results = await asyncio.gather(*(claim_in_tx(db, conversation, uuid4()) for _ in range(10)))
    assert sum(r is not None for r in results) == 1

async def test_claim_after_expired_lease_bumps_version(db, conversation):
    first = await claim_in_tx(db, conversation, uuid4()); await expire_lease(db, conversation)
    second = await claim_in_tx(db, conversation, uuid4())
    assert second.claim_version == first.claim_version + 1

async def test_fenced_write_fails_after_takeover(db, conversation):
    first = await claim_in_tx(db, conversation, uuid4()); await expire_lease(db, conversation)
    await claim_in_tx(db, conversation, uuid4())
    with pytest.raises(ClaimLost):
        async with fenced(db, first) as conn:
            await conn.execute(text("UPDATE support.conversations SET turn_count = 99 WHERE id = :c"), {"c": conversation})
    assert await turn_count(db, conversation) == 0

async def test_assert_claim_after_takeover(db, conversation):
    first = await claim_in_tx(db, conversation, uuid4()); await assert_claim(db, first)   # no exception
    await expire_lease(db, conversation); await claim_in_tx(db, conversation, uuid4())
    with pytest.raises(ClaimLost): await assert_claim(db, first)
```

`expire_lease` is a test helper (`UPDATE … SET active_turn_lease_expires_at = now() - interval '1 second'`), shared
via `conftest.py`.

- [ ] **Step 2–4:** FAIL → implement → PASS
- [ ] **Step 5: Commit** — `feat(support): fenced turn claims`

---

### Task 9: Identity — normalization, attempt slots, outcome transaction, assurance

**Files:**
- Create: `src/support_api/identity/{normalize.py,attempts.py,assurance.py}`
- Test: `tests/unit/test_normalize.py`, `tests/integration/test_attempts.py`

**Interfaces:**
- Consumes: `Claim`, `fenced`, `ClaimLost` (Task 8); `Database` (Task 5).
- Produces:

```python
@dataclass(frozen=True)
class NormalizedIdentifier: kind: Literal["cpf", "account"]; digits: str
def normalize(raw: str) -> NormalizedIdentifier | None          # 11 digits → cpf, 10 → account, else None
def lockout_key(n: NormalizedIdentifier, key: bytes) -> str     # hex HMAC-SHA256(key, f"{kind}:{digits}")

class AdmissionRejected(Exception): code: Literal["locked", "verification_busy"]
async def admit(conn: AsyncConnection, claim: Claim, identifier_hash: str) -> UUID   # inside fenced(); returns attempt_id
Outcome = Literal["failed", "verified", "upstream_error"]
async def record_outcome(db: Database, attempt_id: UUID, outcome: Outcome) -> bool    # unfenced, idempotent
async def apply_verified(db: Database, claim: Claim, attempt_id: UUID, customer_ref: str, first_name: str) -> bool
    # True: outcome + assurance committed under the claim; False: claim lost → record_outcome only

@dataclass(frozen=True)
class Assurance: level: Literal["none", "kba", "step_up"]; customer_ref: str | None; expired: bool
async def current_assurance(conn: AsyncConnection, conversation_id: UUID) -> Assurance   # uses DB now()
async def conversation_locked(conn, conversation_id) -> bool
```

`admit` follows §5.2 step 1 for the identifier, then the same check for the conversation. Lock order: the
conversation row (already locked by `fenced`), then `identifier_lockouts`, then `conversation_auth`. Window reset:
if `window_started_at` is null or older than 15 min, set `failed_count=0, window_started_at=now()`.
`record_outcome` is §5.2's attempt-outcome transaction verbatim: it starts with `UPDATE verification_attempts SET
status=:outcome WHERE id=:id AND status IN ('in_flight','abandoned') RETURNING identifier_hash, conversation_id`.
`apply_verified` sets `assurance_level='kba', verified_at=now(), verified_expires_at=now()+30 min`.

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.parametrize("raw", ["123.456.789-09", "12345678909", " 123 456 789 09 "])
def test_cpf_formats_share_key(raw):
    assert lockout_key(normalize(raw), KEY) == lockout_key(normalize("12345678909"), KEY)
def test_invalid_lengths_return_none(): assert normalize("123") is None and normalize("1" * 12) is None

async def test_three_failures_lock_identifier_and_conversation(db, claimed_conversation): ...
async def test_window_resets_after_15_minutes(db, claimed_conversation): ...  # backdate window_started_at
async def test_upstream_error_frees_slot_without_counting(db, claimed_conversation): ...   # AC 7
async def test_record_outcome_is_idempotent(db, attempt):
    assert await record_outcome(db, attempt, "failed") is True
    assert await record_outcome(db, attempt, "failed") is False
    assert (await counters(db, attempt)).identifier_failed == 1

async def test_concurrent_admission_caps_in_flight_at_three(db, ten_claimed_conversations):  # AC 8, service level
    results = await asyncio.gather(*(admit_in_fenced(db, c, HASH) for c in ten_claimed_conversations), return_exceptions=True)
    assert sum(isinstance(r, UUID) for r in results) == 3
    assert all(r.code == "verification_busy" for r in results if isinstance(r, AdmissionRejected))

async def test_late_failure_after_claim_lost_counts_on_both(db, claimed_conversation_with_two_failures):
    attempt = await admit_in_fenced(db, claimed_conversation_with_two_failures, HASH)
    await take_over(db, claimed_conversation_with_two_failures)       # expire lease + claim by another turn
    assert await record_outcome(db, attempt, "failed") is True
    c = await counters(db, attempt)
    assert c.identifier_failed == 3 and c.identifier_locked and c.conversation_failed == 3 and c.conversation_locked

async def test_apply_verified_after_claim_lost_leaves_assurance(db, claimed_conversation):
    attempt = await admit_in_fenced(db, claimed_conversation, HASH); await take_over(db, claimed_conversation)
    assert await apply_verified(db, claimed_conversation, attempt, "cus_a", "Ana") is False
    assert await attempt_status(db, attempt) == "verified"
    assert (await assurance(db, claimed_conversation)).level == "none"

async def test_assurance_expires_after_30_minutes(db, verified_conversation): ...  # backdate verified_expires_at
```

- [ ] **Step 2–4:** FAIL → implement → PASS
- [ ] **Step 5: Commit** — `feat(identity): atomic attempt slots, idempotent outcomes, assurance`

---

### Task 10: Tools and executor

**Files:**
- Create: `src/support_api/tools/{schemas.py,executor.py}`
- Test: `tests/unit/test_executor.py`, `tests/integration/test_executor_billing.py`

**Interfaces:**
- Consumes: `BillingClient` (Task 7); identity functions (Task 9); `Claim`, `fenced` (Task 8); `Deadline`.
- Produces:

```python
ToolErrorCode = Literal["invalid_arguments", "authentication_required", "authentication_failed", "locked",
    "verification_busy", "not_found", "upstream_timeout", "upstream_unavailable", "internal_error"]
class ToolResult(BaseModel): data: dict | list; record_count: int
class ToolError(BaseModel): code: ToolErrorCode
@dataclass
class ToolContext: claim: Claim; deadline: Deadline; checkpoints: Checkpoints
@dataclass
class ToolTrace: name: str; status: str; error_code: str | None; attempts: int; duration_ms: int; record_count: int
class Executor:
    def __init__(self, db: Database, billing: BillingClient, identifier_hash_key: bytes): ...
    def tool_definitions(self) -> list[dict]   # Responses API function tools from Pydantic arg models, extra="forbid"
    async def run(self, name: str, raw_arguments: str, ctx: ToolContext) -> tuple[ToolResult | ToolError, ToolTrace]
def to_model_output(result: ToolResult | ToolError) -> str
    # json.dumps({"untrusted_data": data | None, "status": "ok" | code})
```

Arg models follow the §5.3 table. Customer-data tools have no identifier field. `customer_ref` comes from
`current_assurance` on every call, so an expiry mid-turn is caught. Expired assurance → `authentication_required`,
and the trace's error code is `verification_expired`, so `AgentCore` can choose that catalog message.
`authenticate_customer` runs: normalize → `admit` inside `fenced` → `checkpoints.hit("after_verify_admit")` →
`billing.verify(..., guard=claim_guard)` → `apply_verified` / `record_outcome`. If the guard raises `ClaimLost`
before verify is sent, the executor records no outcome. The slot stays `in_flight` until recovery marks it
`abandoned` (§5.5 step 4), so no counter changes. If the claim is lost after verify, the executor raises
`ClaimLost` after recording the outcome. Every billing call passes
`claim_guard = lambda: assert_claim(db, ctx.claim)`. `get_account_summary` exposes only `neighborhood` and `city` from the address. The
executor writes a `tool_calls` row through `fenced`. Unknown tool name → `invalid_arguments`.

- [ ] **Step 1: Write failing tests**

```python
async def test_data_tools_reject_identifier_argument(executor, verified_ctx):  # AC 3
    result, _ = await executor.run("list_bills", '{"customer_ref": "cus_b"}', verified_ctx)
    assert result == ToolError(code="invalid_arguments")
async def test_foreign_bill_id_is_not_found(executor, verified_ctx_a): ...  # B's bill_id → not_found (AC 3)
async def test_data_tool_without_verification(executor, ctx): ...  # authentication_required
async def test_expiry_mid_turn(executor, verified_ctx, support_db):     # Review Focus 3
    await expire_verification(support_db, verified_ctx.claim.conversation_id)
    result, trace = await executor.run("list_bills", "{}", verified_ctx)
    assert result.code == "authentication_required" and trace.error_code == "verification_expired"
async def test_account_summary_hides_street(executor, verified_ctx): ...
async def test_wrong_answers_return_authentication_failed_and_count(executor, ctx, billing_server): ...
async def test_locked_identifier_makes_no_upstream_call(executor, ctx, respx_mock): ...
async def test_claim_lost_before_data_call_sends_nothing(executor, verified_ctx, respx_mock, support_db):
    await take_over(support_db, verified_ctx.claim.conversation_id)
    with pytest.raises(ClaimLost): await executor.run("list_bills", "{}", verified_ctx)
    assert respx_mock.calls.call_count == 0
async def test_model_output_wrapper(): assert json.loads(to_model_output(ToolError(code="not_found"))) == {"untrusted_data": None, "status": "not_found"}
```

- [ ] **Step 2–4:** FAIL → implement → PASS
- [ ] **Step 5: Commit** — `feat(tools): executor with assurance, slots, typed errors`

---

### Task 11: Pricing, model config, live gate

**Files:**
- Create: `src/support_api/usage/pricing.py`
- Test: `tests/unit/test_live_gate.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Rate: input_per_mtok: Decimal; output_per_mtok: Decimal; cached_input_per_mtok: Decimal | None
class Pricing:
    @classmethod
    def load(cls, path: Path) -> "Pricing"
    def rate(self, model: str, tier: str) -> Rate | None    # None unless verified is True and all fields set
    def token_count_unbilled(self) -> bool                  # billed is False and source_url and verified_on set
ReasoningMode = Literal["none", "current_turn", "all_turns"]
class ModelsConfig:
    @classmethod
    def load(cls, path: Path) -> "ModelsConfig"
    def reasoning_items(self, model: str) -> ReasoningMode | None   # None unless verified
def live_gate_failures(settings: Settings, pricing: Pricing, models: ModelsConfig) -> list[str]  # [] = open
class LiveGateClosed(Exception): reasons: list[str]
```

Gate checks match §5.6 items 1–4 exactly, with reasons `live_disabled`, `missing_api_key`, `pricing_unverified`,
`token_count_not_verified_unbilled`, `model_reasoning_unverified`.

- [ ] **Step 1: Write failing tests**

```python
def test_committed_config_keeps_gate_closed(settings_live_with_key):     # real files in config/
    assert set(live_gate_failures(settings_live_with_key, Pricing.load(PRICING), ModelsConfig.load(MODELS))) == {
        "pricing_unverified", "token_count_not_verified_unbilled", "model_reasoning_unverified"}
@pytest.mark.parametrize("billed", [True, None])
def test_billed_or_unknown_counting_closes_gate(billed, verified_fixture_files): ...
def test_fully_verified_fixture_opens_gate(verified_fixture_files): assert live_gate_failures(...) == []
def test_disabled_flag_alone_closes_gate(...): ...
```

Verified fixture files live in `tests/fixtures/{pricing_verified.json,models_verified.json}` with fictitious rates
marked `"source_url": "test-fixture"`. They are test-only and never copied into `config/`.

- [ ] **Step 2–4:** FAIL → implement → PASS
- [ ] **Step 5: Commit** — `feat(usage): pricing/model config and live gate`

---

### Task 12: Budget reservations and ledger

**Files:**
- Create: `src/support_api/usage/budget.py`
- Test: `tests/integration/test_budget.py`

**Interfaces:**
- Consumes: `Claim`, `fenced` (Task 8); `Rate` (Task 11).
- Produces:

```python
@dataclass(frozen=True)
class Usage: input_tokens: int; cached_input_tokens: int; output_tokens: int
def worst_case_usd(input_tokens: int, max_output_tokens: int, rate: Rate) -> Decimal      # ROUND_CEILING, 6 dp
def actual_usd(usage: Usage, rate: Rate) -> Decimal
class BudgetExceeded(Exception): scope: Literal["project", "conversation"]
# Fenced worker operations: call inside `async with fenced(db, claim) as conn`. ClaimLost leaves the row untouched.
async def reserve(conn: AsyncConnection, claim: Claim, *, amount: Decimal, kind: str, model: str, tier: str,
                  attempt: int) -> UUID            # returns llm_call_id
async def release(conn: AsyncConnection, claim: Claim, llm_call_id: UUID) -> bool
async def mark_uncertain(conn: AsyncConnection, claim: Claim, llm_call_id: UUID, outcome_class: str,
                         block_reason: str | None) -> bool
# The one unfenced exception (§5.5): own-row settlement with usage actually received.
async def settle(db: Database, llm_call_id: UUID, usage: Usage, rate: Rate) -> bool
# Recovery: runs in the caller's transaction, which already holds the conversation row lock.
async def recover_reservations(conn: AsyncConnection, conversation_id: UUID, turn_id: UUID) -> int
```

`reserve` runs the two conditional `UPDATE … WHERE spent_usd + reserved_usd + uncertain_usd + :amt <= cap_usd
RETURNING` statements (project first, then conversation). It creates the `conversation_budgets` row on demand with
`CONVERSATION_BUDGET_USD`, then inserts `llm_calls` with status `reserved`. Every ledger operation starts with
a status-conditional `UPDATE llm_calls … WHERE id=:id AND status IN (…) RETURNING reserved_usd` on its own row.
Zero rows means it was already applied: return `False` and leave the budget rows untouched. The operations are:
- `release`: `reserved → released`. It subtracts the amount from `reserved_usd` on both budget rows. It applies
  only from `reserved` and never from `uncertain`.
- `mark_uncertain`: `reserved → uncertain`. It moves the amount from `reserved_usd` to `uncertain_usd` on both
  rows. If `block_reason` is set, it writes `conversations.live_blocked_reason`, which only ever goes from null
  to set.
- `settle`: `reserved|uncertain → settled`. It removes the amount from whichever bucket held it and adds
  `actual_usd` to `spent_usd`. It needs no claim, so a stale worker that received usage still records spend.
- `recover_reservations`: it applies `mark_uncertain`'s transition to every `reserved` row of `turn_id` and
  sets `live_blocked_reason = 'interrupted_after_possible_send'` when any row moved. It returns the number of
  rows moved. Task 17's `recover_conversation` calls it for §5.5 step 3.

A fenced operation the worker could not run because of `ClaimLost` is safe: recovery has moved or will move that
row to `uncertain`, which is the conservative state.

- [ ] **Step 1: Write failing tests**

```python
async def test_concurrent_reservations_never_exceed_conversation_cap(db, claimed):  # AC 15
    results = await asyncio.gather(*(reserve_in_fenced(db, claimed, Decimal("0.03")) for _ in range(10)), return_exceptions=True)
    assert sum(isinstance(r, UUID) for r in results) == 3
    assert (await conv_budget(db, claimed)).reserved_usd == Decimal("0.09")
async def test_concurrent_reservations_never_exceed_project_cap(db, many_claimed, project_cap_small): ...
async def test_settle_moves_reserved_to_spent(db, reservation): ...
async def test_uncertain_never_released_and_counts_against_cap(db, reservation): ...
async def test_settle_from_uncertain(db, reservation): ...   # stale worker with usage
async def test_settle_after_takeover_still_applies(db, reservation): ...   # take_over, recover_reservations, settle → settled
async def test_release_and_mark_uncertain_fenced_after_takeover(db, reservation):
    await take_over(db, reservation.claim.conversation_id)
    for op in (lambda c: release(c, reservation.claim, reservation.id),
               lambda c: mark_uncertain(c, reservation.claim, reservation.id, "unknown", "provider_outcome_unknown")):
        with pytest.raises(ClaimLost):
            async with fenced(db, reservation.claim) as conn: await op(conn)
    assert (await llm_call(db, reservation.id)).status == "reserved"   # untouched until recovery
async def test_release_never_applies_to_uncertain(db, reservation): ...    # mark_uncertain then release → False, amounts unchanged
async def test_recover_reservations_uses_caller_transaction(db, reservation):
    async with db.begin() as conn:
        await lock_conversation(conn, reservation.claim.conversation_id)
        assert await recover_reservations(conn, reservation.claim.conversation_id, reservation.claim.turn_id) == 1
        await conn.rollback()                                          # caller owns the transaction
    assert (await llm_call(db, reservation.id)).status == "reserved"
async def test_ledger_ops_idempotent(db, reservation): ...         # second settle / mark_uncertain → False, no double count
async def test_worst_case_rounds_up(): assert worst_case_usd(1, 0, Rate(Decimal("0.0000001"), Decimal(0), None)) == Decimal("0.000001")
```

- [ ] **Step 2–4:** FAIL → implement → PASS
- [ ] **Step 5: Commit** — `feat(usage): atomic dual-cap reservations and ledger`

---

### Task 13: Provider outcome classification

**Files:**
- Create: `src/support_api/usage/classify.py`
- Test: `tests/unit/test_classify.py`

**Interfaces:**
- Produces:

```python
class OutcomeClass(StrEnum):
    SETTLED = "settled"; UNPRICED_TIER = "unpriced_tier"; CONNECT_FAILED = "connect_failed"
    RATE_LIMITED = "rate_limited"; QUOTA = "quota"; REJECTED = "rejected"; UNKNOWN = "unknown"
@dataclass(frozen=True)
class Decision: outcome: OutcomeClass; ledger: Literal["settle", "release", "uncertain"]; block_reason: str | None; retryable: bool
def classify_response(*, has_usage: bool, tier_priced: bool) -> Decision
def classify_error(*, exc: BaseException | None, status: int | None, error_type: str | None, error_code: str | None) -> Decision
def rate_limit_delay(retry_after: str | None, remaining_s: float, min_attempt_s: float = 5.0) -> float | None
```

The mapping is the §5.6 table, row for row. Block reasons: `provider_quota`, `provider_rejected`,
`provider_outcome_unknown`, and `unpriced_tier` for an unpriced tier. `CONNECT_FAILED` only when the underlying cause
is `httpx.ConnectError` or `httpx.ConnectTimeout` (walk `__cause__`/`__context__`). A missing response with no
connect error is `UNKNOWN`. `rate_limit_delay`: a numeric `Retry-After` gives seconds; absent or non-numeric gives
1.0. It returns `None` unless `delay + min_attempt_s <= remaining_s`.

- [ ] **Step 1: Write failing tests** — one parametrized case per §5.6 row (AC 16, classification half):

```python
@pytest.mark.parametrize("status,etype,ecode,expected", [
    (429, "rate_limit_error", None, ("uncertain", None, True)),
    (429, None, "slow_down", ("uncertain", None, True)),
    (429, "insufficient_quota", None, ("uncertain", "provider_quota", False)),
    (429, None, "project_spend_limit_exceeded", ("uncertain", "provider_quota", False)),
    (429, None, "something_new", ("uncertain", "provider_quota", False)),       # unclassified 429
    (400, None, None, ("uncertain", "provider_rejected", False)),
    (401, None, None, ("uncertain", "provider_rejected", False)),
    (403, None, None, ("uncertain", "provider_rejected", False)),
    (500, None, None, ("uncertain", "provider_outcome_unknown", False)),
    (503, None, None, ("uncertain", "provider_outcome_unknown", False)),
])
def test_http_error_rows(status, etype, ecode, expected): ...
def test_connect_error_releases_and_retries(): ...            # httpx.ConnectError cause → ("release", None, True)
def test_read_timeout_is_unknown(): ...                       # httpx.ReadTimeout → uncertain, blocked, no retry
def test_unpriced_tier_blocks(): ...
def test_rate_limit_delay_respects_deadline():
    assert rate_limit_delay("2", 10) == 2 and rate_limit_delay("6", 10) is None and rate_limit_delay(None, 6) == 1.0
```

- [ ] **Step 2–4:** FAIL → implement → PASS
- [ ] **Step 5: Commit** — `feat(usage): provider outcome classification`

---

### Task 14: History assembly (unit level, test 18 core)

**Files:**
- Create: `src/support_api/agent/history.py`
- Test: `tests/unit/test_history.py`

**Interfaces:**
- Consumes: `ReasoningMode` (Task 11).
- Produces:

```python
@dataclass(frozen=True)
class Item: turn_id: UUID; seq: int; kind: str; call_id: str | None; model: str | None; payload: dict
@dataclass(frozen=True)
class TurnItems: turn_id: UUID; status: str; items: tuple[Item, ...]
class HistoryInvariantError(Exception): ...
async def load_past_turns(conn: AsyncConnection, conversation_id: UUID, exclude_turn_id: UUID) -> list[TurnItems]
def select_turns(past: list[TurnItems], *, max_turns: int, token_budget: int, estimate: Callable[[Item], int]) -> list[TurnItems]
def to_request_input(turns: list[TurnItems], current: list[Item], *, mode: ReasoningMode, model: str) -> list[dict]
def check_tool_pairs(input_items: list[dict], pending_call_ids: frozenset[str] = frozenset()) -> None
def estimate_tokens(item: Item) -> int     # len(json.dumps(payload, ensure_ascii=False)) // 3 + 4; selection only
```

Rules (§5.4.1):
- `select_turns` keeps the newest turns that fit both `max_turns` and `token_budget`, whole turns only.
- `to_request_input` emits, for `completed` turns, all items in `seq` order. For `interrupted`/`failed` turns it emits
  only `user_message` and `assistant_message`.
- It then filters `reasoning`: omit when `mode != "all_turns"` for past turns, and always when `item.model !=
  model`. Current-turn reasoning items are kept unless `item.model != model`.
- `load_past_turns` never mutates storage, and `to_request_input` is pure.

- [ ] **Step 1: Write failing tests** (builder `conv()` creates `TurnItems` fixtures: T1 plain completed; T2 completed
  with `reasoning` + two tool exchanges `c1`, `c2`; T3 interrupted with dangling `function_call` `c3`; T4 plain
  completed; one T2 reasoning item made by `other-model`)

```python
@pytest.mark.parametrize("mode", ["none", "current_turn", "all_turns"])
def test_no_orphans_and_interrupted_tools_omitted(mode):
    items = to_request_input(conv(), current=[user("e a conta de luz?")], mode=mode, model="m")
    check_tool_pairs(items)                                         # no exception
    assert not any(i.get("call_id") == "c3" for i in items)
    assert texts(items).count("Não foi possível concluir sua mensagem. Envie novamente para tentar outra vez.") == 1

def test_reasoning_filtering_by_mode():
    assert kinds(to_request_input(conv(), [user("x")], mode="current_turn", model="m")).count("reasoning") == 0
    assert kinds(to_request_input(conv(), [user("x")], mode="all_turns", model="m")).count("reasoning") == 1  # other-model item dropped

def test_current_turn_reasoning_kept_in_current_turn_mode():
    current = [user("x"), reasoning("m"), call("c9"), output("c9")]
    assert kinds(to_request_input([], current, mode="current_turn", model="m")).count("reasoning") == 1

def test_truncation_drops_whole_oldest_turns():
    kept = select_turns(conv(), max_turns=6, token_budget=size_of(conv()[1:]), estimate=estimate_tokens)
    assert [t.turn_id for t in kept] == [t.turn_id for t in conv()[1:]]
    kept = select_turns(conv(), max_turns=6, token_budget=size_of(conv()[2:]), estimate=estimate_tokens)
    ids = {i.get("call_id") for i in to_request_input(kept, [user("x")], mode="all_turns", model="m")}
    assert {"c1", "c2"}.isdisjoint(ids)                             # T2's exchanges and reasoning gone together

def test_max_turns_bound(): assert len(select_turns(conv() * 3, max_turns=6, token_budget=10**9, estimate=estimate_tokens)) == 6

def test_check_tool_pairs_rejects_orphans():
    with pytest.raises(HistoryInvariantError): check_tool_pairs([out_dict("cX")])
    with pytest.raises(HistoryInvariantError): check_tool_pairs([call_dict("cY")])
    check_tool_pairs([call_dict("cY")], pending_call_ids=frozenset({"cY"}))
```

- [ ] **Step 2–4:** FAIL → implement → PASS
- [ ] **Step 5: Commit** — `feat(agent): complete-turn history assembly with reasoning filtering`

---

### Task 15: ChatModel protocol, ScriptedModel, queued fake

**Files:**
- Create: `src/support_api/agent/models/{base.py,scripted.py,fake.py}`, `src/support_api/agent/format_ptbr.py`,
  `prompts/system_ptbr.md`
- Test: `tests/unit/test_scripted_model.py`, `tests/unit/test_format_ptbr.py`

**Interfaces:**
- Consumes: `Item` (Task 14); tool names and arg schemas (Task 10).
- Produces:

```python
@dataclass(frozen=True)
class FunctionCall: call_id: str; name: str; arguments: str
@dataclass(frozen=True)
class ModelRequest: instructions: str; input: tuple[dict, ...]; tools: tuple[dict, ...]
@dataclass(frozen=True)
class ModelResponse:
    status: Literal["completed", "incomplete"]; text: str | None
    function_calls: tuple[FunctionCall, ...]; output_items: tuple[dict, ...]   # all items incl. reasoning, for persistence
@dataclass
class ModelContext: claim: Claim; deadline: Deadline; checkpoints: Checkpoints; trace: list[dict]
class ModelUnavailable(Exception): catalog_key: Literal["upstream_unavailable", "live_blocked", "limit_reached"]
class ChatModel(Protocol):
    name: str
    async def count_input_tokens(self, request: ModelRequest, ctx: ModelContext) -> int
    async def generate(self, request: ModelRequest, ctx: ModelContext) -> ModelResponse
def format_brl(amount: Decimal) -> str      # "R$ 1.234,56"
def format_date(d: date) -> str             # "05/11/2026"
```

- `ScriptedModel.count_input_tokens` returns the local estimate and sends nothing. `generate` is deterministic and
  reads only `request.input`. A tool output's tool name and arguments come from the `function_call` item with the
  same `call_id`. "Bill intent" means the text matches `fatura|conta|boleto|pagar|débito|vencimento`. The
  requested status comes from that text: "aberto" → `open`, "atrasad" → `overdue`, "pag" → `paid`, else none.
  The rules are checked in this order, and the first match wins:
  1. **Authentication succeeded.** The last item is an `authenticate_customer` output with `"status": "ok"`.
     If either of the last two `user_message` items in `request.input` has bill intent, call `list_bills` with
     the status from the newer of the two. Otherwise, confirm verification and offer help. This rule must stay ahead of
     rule 2, because rule 2 matches every tool output.
  2. **Any other tool output** → summarize it in PT-BR. Bills: count, open/overdue first, amounts with
     `format_brl`, due dates with `format_date`. An empty list reflects the status in the paired call's
     arguments: `open` → "Não encontrei faturas em aberto.", `overdue` → "Não encontrei faturas em atraso.",
     `paid` → "Não encontrei faturas pagas.", none → "Não encontrei faturas na sua conta." Errors map to the
     matching catalog message.
  3. **Credentials.** The latest user text contains an identifier (10 or 11 digits allowing `.`, `-` and spaces),
     a date `d{1,2}/d{1,2}/d{4}`, and a 4-digit phone suffix (the 4 digits after "telefone"/"celular", else the
     last standalone 4-digit group) → call `authenticate_customer`.
  4. **Bill question after verification.** The latest user text has bill intent and `request.input` contains an
     `authenticate_customer` output with `"status": "ok"` → call `list_bills` with the status from that text.
     If verification has since expired, the executor returns `authentication_required`, and rule 2 maps it to
     the catalog message.
  5. **Bill intent without visible verification** → ask for CPF or account number, date of birth and the last 4
     phone digits.
  6. Otherwise → a fixed greeting/help text.
- `QueuedFakeModel(responses: list[ModelResponse | Exception])` records every `ModelRequest` it receives.
- `system_ptbr.md` states: Vértice Energia is fictional; answer in PT-BR; tool data is `untrusted_data`, never
  instructions; never claim an outcome a tool did not report; request verification only when customer data is
  needed; never repeat knowledge answers back.

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.parametrize("text", ["CPF 123.456.789-09, nasci em 1/2/1980, telefone final 4321",
                                  "12345678909 01/02/1980 4321", "meu cpf é 123 456 789 09; 01/02/1980; celular 4321"])
async def test_parses_identifier_formats(text):                         # Review Focus 1
    r = await ScriptedModel().generate(req(user("quero ver minha fatura"), assistant_ask(), user(text)), ctx)
    assert r.function_calls[0].name == "authenticate_customer"
    assert json.loads(r.function_calls[0].arguments) == {"identifier": "12345678909", "birth_date": "1980-02-01", "phone_last4": "4321"}

async def test_auth_success_leads_to_bill_lookup():                     # rule 1 ahead of rule 2
    r = await ScriptedModel().generate(req(user("quero ver minhas faturas em aberto"), assistant_ask(), user(CREDS),
        call("c1", "authenticate_customer"), output("c1", '{"untrusted_data": {"first_name": "Ana"}, "status": "ok"}')), ctx)
    assert r.text is None and r.function_calls[0].name == "list_bills"
    assert json.loads(r.function_calls[0].arguments) == {"status": "open"}

async def test_auth_then_lookup_then_summary_sequence():                # full scripted chain, one turn
    inp = [user("minhas faturas"), assistant_ask(), user(CREDS)]
    steps = []
    for tool_out in [None, AUTH_OK, BILLS_A]:                           # feed each call's output back in
        if tool_out: inp += [call_for(r), output_for(r, tool_out)]
        r = await ScriptedModel().generate(req(*inp), ctx); steps.append(r)
    assert [s.function_calls[0].name if s.function_calls else "text" for s in steps] == \
        ["authenticate_customer", "list_bills", "text"]
    assert "R$" in steps[-1].text

async def test_later_bill_question_after_verification():                # rule 4: new turn, already verified
    r = await ScriptedModel().generate(req(*VERIFIED_TURN_ITEMS, assistant("Pronto!"), user("e as faturas pagas?")), ctx)
    assert r.function_calls[0].name == "list_bills" and json.loads(r.function_calls[0].arguments) == {"status": "paid"}

@pytest.mark.parametrize("status,expected", [
    ("open", "Não encontrei faturas em aberto."), ("overdue", "Não encontrei faturas em atraso."),
    ("paid", "Não encontrei faturas pagas."), (None, "Não encontrei faturas na sua conta.")])
async def test_empty_bills_reflect_requested_status(status, expected):  # Review Focus 2
    args = {} if status is None else {"status": status}
    r = await ScriptedModel().generate(req(..., call("c2", "list_bills", args), output("c2", '{"untrusted_data": [], "status": "ok"}')), ctx)
    assert expected in r.text and "R$" not in r.text

async def test_summary_uses_ptbr_formats(): ...   # "R$ 1.234,56", "05/11/2026"
async def test_asks_for_verification_on_bill_intent(): ...
async def test_scripted_count_sends_nothing(respx_mock): ...            # respx_mock has no routes; asserts zero calls
def test_format_brl(): assert format_brl(Decimal("1234.5")) == "R$ 1.234,50"
```

- [ ] **Step 2–4:** FAIL → implement → PASS
- [ ] **Step 5: Commit** — `feat(agent): ChatModel protocol, deterministic PT-BR scripted model`

---

### Task 16: AgentCore bounded loop

**Files:**
- Create: `src/support_api/agent/core.py`
- Test: `tests/integration/test_agent_core.py`

**Interfaces:**
- Consumes: `ChatModel`, `ModelContext`, `ModelUnavailable` (Task 15); `Executor`, `to_model_output` (Task 10);
  history functions (Task 14); `fenced`, `ClaimLost` (Task 8); catalog.
- Produces:

```python
@dataclass
class TurnOutcome:
    status: Literal["completed", "failed"]; reply: str; trace: dict   # {tool_calls: [...], model_calls: [...]}
    final_items: tuple[dict, ...]   # the final response's non-message items (e.g. reasoning); () for catalog endings
class AgentCore:
    def __init__(self, db: Database, model: ChatModel, executor: Executor, settings: Settings,
                 checkpoints: Checkpoints = Checkpoints()): ...
    async def handle_turn(self, claim: Claim, text: str, channel: Literal["chat"], deadline: Deadline) -> TurnOutcome
```

The loop: load past turns → `select_turns` (budget `MAX_INPUT_TOKENS` minus the current-turn estimate; if the
current turn alone exceeds it → `failed`/`limit_reached` before any model call) → build request → `count` → if over
`MAX_INPUT_TOKENS`, drop the oldest selected turn and count once more → if still over → `limit_reached` → `generate`.
After each response, call `checkpoints.hit("after_model_response")`.

**Transcript ownership.** Each `conversation_items` row has exactly one writer:

| Items | Writer |
|---|---|
| `user_message` | turn service, submission transaction (Task 17 steps 1–4) |
| `reasoning`, `function_call` from a tool-calling response; `function_call_output` | `AgentCore`, fenced, as below |
| Final response items (`reasoning` + the single `assistant_message`) or the catalog `assistant_message` | turn service, completion transaction (Task 17 step 6) |
| Catalog `assistant_message` of a recovered turn | `recover_conversation` (Task 17) |

Step 6 and recovery can't both write for the same turn. Step 6 is fenced, and recovery requires the claim to
have been taken over. `AgentCore` **never** writes an `assistant_message`. For a response that contains
function calls, it persists the `reasoning` and `function_call` items in one fenced transaction **before**
executing any tool call. Any interim `message` item in such a response is dropped and never shown. A worker
that lost its claim then fails with `ClaimLost` before it can send a billing request. Each `function_call_output`
is persisted (fenced) right after its tool runs. For the final response (no function calls), `AgentCore` writes
nothing. It returns `reply` = the response text and `final_items` = its non-message items. Execute function
calls sequentially; each needs a remaining tool budget, otherwise end with `limit_reached`. Append output items
and loop. A final text completes the turn (`completed`). Every catalog-message ending is `failed`. `ModelUnavailable` → its catalog key.
Deadline expiry → `limit_reached`. `ClaimLost` propagates to the caller. Before each request, run
`check_tool_pairs` with `pending_call_ids` set to calls awaiting execution. On a violation, end with `failed`/
`limit_reached` and log `error_code=internal_error`. If `conversations.live_blocked_reason` is set and the model is
live, end with `live_blocked` before counting. Limits come from Settings: model calls 5, tool calls 4, output 800.

- [ ] **Step 1: Write failing tests** (`QueuedFakeModel` + respx billing)

```python
async def test_tool_call_limit_ends_with_limit_message(core_with_fake): ...   # 5 calls requested → 4 run, failed, MESSAGES["limit_reached"]
async def test_model_call_limit(core_with_fake): ...                          # 6th model call never made
async def test_deadline_respected_on_slow_billing(core, respx_slow): ...      # Deadline(45) with fake clock; outcome ≤ 45 s
async def test_billing_errors_give_recoverable_message_no_fabrication(core, respx_500): ...  # reply == MESSAGES["upstream_unavailable"], trace error_code
async def test_current_turn_too_large_sends_no_model_request(core_with_fake): ...  # fake.requests == []
async def test_output_items_persisted_in_order(core_with_fake, support_db): ...  # reasoning, function_call, function_call_output
async def test_core_never_writes_assistant_message(core_with_fake, support_db):
    outcome = await core_with_fake.handle_turn(...)                   # tool call, then final text
    assert "assistant_message" not in await items_for(support_db, TURN) and outcome.reply == "Certo!"
async def test_expired_verification_uses_expired_message(core_with_fake, support_db): ...
```

- [ ] **Step 2–4:** FAIL → implement → PASS
- [ ] **Step 5: Commit** — `feat(agent): bounded agent loop`

---

### Task 17: Turn service, duplicates, crash recovery (tests 12, 13, 14)

**Files:**
- Create: `src/support_api/turns/{service.py,recovery.py}`
- Modify: `channels/chat.py` (messages endpoint, transcript), `app.py` (startup sweep in lifespan)
- Test: `tests/integration/test_turn_service.py`, `tests/integration/test_recovery.py`

**Interfaces:**
- Consumes: `try_claim`, `fenced`, `release`, `ClaimLost` (Task 8); `AgentCore`, `TurnOutcome` (Task 16);
  `recover_reservations` (Task 12); catalog.
- Produces:

```python
@dataclass
class SubmitResult: http_status: int; body: dict
async def submit_message(db: Database, core: AgentCore, *, session_id: UUID, conversation_id: UUID,
                         client_message_id: UUID, text: str, correlation_id: str) -> SubmitResult
async def recover_conversation(conn: AsyncConnection, conversation_id: UUID) -> bool   # caller holds conversation row lock
async def startup_sweep(db: Database) -> int
```

- `POST /api/conversations/{id}/messages` body: `{client_message_id: UUID, text: str}`. `len(text)` counts code
  points; 1–2,000 allowed, otherwise 422 `text_length`.
- Response: `200 {"turn_id", "status", "reply", "trace"}`. Errors: `409 {"error": "turn_in_progress", "message":
  MESSAGES["turn_in_progress"]}`, `422 {"error": "client_message_id_conflict"}`, 404 as in Task 6.
- Turn 31 → `failed` with `limit_reached`.
- `submit_message` flow. Steps 1–4 are **one transaction**: the conversation row lock taken in step 1 is held
  until the commit in step 4, so recovery, the duplicate lookup and the claim can't interleave with another
  submission.
  1. Lock the conversation row (`FOR UPDATE`) and run `recover_conversation` if the lease expired.
  2. Look up `(conversation_id, client_message_id)` and apply the §5.5 rules. An early return (stored reply,
     409, 422) commits any recovery done in step 1.
  3. `try_claim`. Then insert the turn row and the `user_message` item, and increment `turn_count`.
  4. Commit.
  5. Call `core.handle_turn` under `Deadline(45)`, outside any transaction.
  6. In one fenced transaction: write `outcome.final_items`, then the turn's single `assistant_message` item
     (`outcome.reply`, whether model text or catalog), the turn's `reply` and status, then `release`. This is the
     only place a non-recovered turn gets its `assistant_message` (Task 16 transcript ownership).
  - `ClaimLost` during 5–6 → return the stored turn state after running recovery (status `interrupted`).
  - `SimulatedCrash` propagates (test only).
- `recover_conversation` runs §5.5 recovery steps 1–5 exactly, inside the caller's transaction. It writes the
  catalog reply as the turn's `reply` and as its `assistant_message` item. It calls
  `recover_reservations(conn, conversation_id, turn_id)` (Task 12) for step 3, and marks in-flight attempts
  `abandoned`.
- `GET /api/conversations/{id}` also runs lazy recovery, then returns turns with their status, reply and trace
  metadata (no tool payloads).

- [ ] **Step 1: Write failing tests**

```python
async def test_duplicate_semantics(api):   # AC 11
    mid = uuid4(); first = await api.send(mid, "olá")
    assert (await api.send(mid, "olá")).json() == first.json() and api.model.requests_count == 1
    assert (await api.send(mid, "outra")).status_code == 422
async def test_concurrent_identical_submissions_one_turn(api): ...   # gather 5 → one turn row; others 409 or stored reply
async def test_concurrent_different_messages_one_409(api, slow_model): ...
async def test_text_length_code_points(api):                           # Review Focus 4
    assert (await api.send(uuid4(), "ç" * 2000)).status_code == 200
    assert (await api.send(uuid4(), "😀" * 2001)).status_code == 422
async def test_turn_limit_30(api): ...
async def test_exactly_one_assistant_message_per_turn(api, support_db):   # single transcript owner
    for text in ["minhas faturas", CREDS_A, "e as pagas?"]: await api.send(uuid4(), text)   # scripted, incl. tools
    for turn in await turns(support_db, api.conversation_id):
        assert (await items_for(support_db, turn.id)).count("assistant_message") == 1

async def test_crash_after_send(api_live_mocked, support_db, crash_at):   # AC 12
    crash_at("after_send")                    # Checkpoints raises SimulatedCrash after the mocked generation request
    mid = uuid4()
    with pytest.raises(SimulatedCrash): await api_live_mocked.send(mid, "minhas faturas")
    assert api_live_mocked.openai_routes["responses"].call_count == 1
    await expire_lease(support_db, api_live_mocked.conversation_id)
    replay = await api_live_mocked.send(mid, "minhas faturas")
    assert replay.status_code == 200 and replay.json()["status"] == "interrupted"
    assert replay.json()["reply"] == MESSAGES["turn_interrupted"]
    call = await only_llm_call(support_db, kind="generation")
    assert call.status == "uncertain" and call.reserved_usd > 0
    assert await live_blocked_reason(support_db, api_live_mocked.conversation_id) == "interrupted_after_possible_send"
    nxt = await api_live_mocked.send(uuid4(), "e agora?")
    assert nxt.json()["reply"] == MESSAGES["live_blocked"] and api_live_mocked.openai_routes["responses"].call_count == 1

async def test_startup_sweep_recovers_without_request(api_live_mocked, support_db, crash_at): ...  # same end state via startup_sweep()

async def test_stale_worker_cannot_write(api_verified_fake_model, support_db, pause_at):   # AC 13
    # Fake model: turn A's first response is a list_bills call; turn B's response is plain text.
    api = api_verified_fake_model
    gate = pause_at("after_model_response", turn=1)   # worker A pauses after its first model response
    a = asyncio.create_task(api.send(uuid4(), "minhas faturas")); await gate.reached.wait()
    await expire_lease(support_db, api.conversation_id)
    b = await api.send(uuid4(), "outra pergunta")                      # B recovers A's turn and claims
    gate.release(); a_result = await a
    assert a_result.json()["status"] == "interrupted" and b.json()["status"] == "completed"
    assert api.billing_routes["list_bills"].call_count == 0            # A's fenced persist failed before the tool ran
    assert await items_for(support_db, turn_id(a_result)) == ["user_message", "assistant_message"]
    assert await item_turn_ids(support_db, api.conversation_id) == {turn_id(a_result), turn_id(b)}
```

Test 14 (AC 14), late verification outcome after takeover:

```python
async def test_late_verification_failure_after_takeover(api_fake_model, support_db, billing_gate):
    # Fake model issues authenticate_customer with wrong answers for persona A on each turn.
    await api_fake_model.send(uuid4(), "verificar"); await api_fake_model.send(uuid4(), "verificar")
    assert (await conv_auth(support_db)).failed_attempts == 2
    billing_gate.hold()                                               # respx verify route awaits an Event, then {"verified": False}
    a = asyncio.create_task(api_fake_model.send(mid_a := uuid4(), "verificar"))
    await billing_gate.request_seen.wait()
    await expire_lease(support_db, api_fake_model.conversation_id)
    async with support_db.begin() as conn:
        await lock_conversation(conn, api_fake_model.conversation_id)
        assert await recover_conversation(conn, api_fake_model.conversation_id) is True
    billing_gate.release(); await a

    attempt = await last_attempt(support_db)
    assert attempt.status == "failed"
    ident = await identifier_row(support_db, PERSONA_A_HASH)
    auth = await conv_auth(support_db)
    assert ident.failed_count == 3 and ident.locked_until is not None
    assert auth.failed_attempts == 3 and auth.locked_until is not None
    assert auth.assurance_level == "none"
    assert (await turn_by_mid(support_db, mid_a)).status == "interrupted"
    assert await items_for(support_db, (await turn_by_mid(support_db, mid_a)).id) == ["user_message", "assistant_message"]

    assert await record_outcome(support_db, attempt.id, "failed") is False   # idempotent replay
    assert (await identifier_row(support_db, PERSONA_A_HASH)).failed_count == 3

async def test_late_verification_success_after_takeover(api_fake_model, support_db, billing_gate):
    # Same setup (two failures), but the held verify returns {"verified": True, ...} for persona A.
    ...
    assert (await last_attempt(support_db)).status == "verified"
    auth = await conv_auth(support_db)
    assert auth.assurance_level == "none" and auth.customer_ref is None and auth.failed_attempts == 2
```

Stale worker paused after verification admission (claim check before the billing attempt):

```python
async def test_takeover_during_after_verify_admit_sends_no_verify(api_fake_model, support_db, pause_at):
    # Fake model issues authenticate_customer with persona A's answers.
    gate = pause_at("after_verify_admit", turn=1)
    a = asyncio.create_task(api_fake_model.send(mid_a := uuid4(), "verificar")); await gate.reached.wait()
    await expire_lease(support_db, api_fake_model.conversation_id)
    async with support_db.begin() as conn:
        await lock_conversation(conn, api_fake_model.conversation_id)
        assert await recover_conversation(conn, api_fake_model.conversation_id) is True
    gate.release(); r = await a
    assert r.json()["status"] == "interrupted"
    assert api_fake_model.billing_routes["verify"].call_count == 0
    attempt = await last_attempt(support_db)
    assert attempt.status == "abandoned"
    assert (await identifier_row(support_db, PERSONA_A_HASH)).failed_count == 0
    assert (await conv_auth(support_db)).failed_attempts == 0
    assert await items_for(support_db, (await turn_by_mid(support_db, mid_a)).id) == ["user_message", "assistant_message"]
```

- [ ] **Step 2: Run — expect FAIL** (`pytest tests/integration/test_turn_service.py tests/integration/test_recovery.py -v`)
- [ ] **Step 3: Implement** `submit_message`, `recover_conversation`, `startup_sweep`, routes. `api_live_mocked` uses
  `OpenAIResponsesModel` from Task 18. Until Task 18 lands, mark `test_crash_after_send` and
  `test_startup_sweep_recovers_without_request` with `pytest.mark.requires_openai_model` (skipped). Task 18 Step 6
  removes the mark.
- [ ] **Step 4: Run — expect PASS** (except the two marked tests, reported as skipped)
- [ ] **Step 5: Commit** — `feat(turns): submission, duplicates, fenced completion, crash recovery`

---

### Task 18: OpenAIResponsesModel (mocked HTTP only; test 18 end-to-end)

**Files:**
- Create: `src/support_api/agent/models/openai_model.py`
- Modify: `app.py` (construct only after `live_gate_failures == []`, else raise `LiveGateClosed` at startup when
  `LLM_PROVIDER=openai`), `tests/conftest.py` (`openai_mock` respx fixture; the network guard already exists
  from Task 5)
- Test: `tests/integration/test_openai_model.py`, `tests/integration/test_history_truncation_e2e.py`,
  `tests/integration/test_live_gate_app.py`

**Interfaces:**
- Consumes: `Pricing`, `ModelsConfig`, `live_gate_failures` (Task 11); `reserve`, `settle`, `release`,
  `mark_uncertain`, `worst_case_usd` (Task 12); `classify_*`, `rate_limit_delay` (Task 13); `ChatModel` (Task 15);
  `fenced`, `assert_claim`, `ClaimLost` (Task 8).
- Produces: `OpenAIResponsesModel(client: openai.AsyncOpenAI, *, db, model: str, tier: str, rate: Rate,
  max_output_tokens: int, count_timeout_s=10, attempt_timeout_s=20)` implementing `ChatModel`, plus
  `build_count_body(req) -> dict` and `build_generation_body(req) -> dict`. Both bodies come from one builder.
  Generation adds `max_output_tokens`, `store=False`, `service_tier`, `include=["reasoning.encrypted_content"]`.
  Shared fields: `model`, `instructions`, `input`, `tools`, `tool_choice="auto"`.
- `count_input_tokens`: `await assert_claim(db, ctx.claim)`, then `POST /v1/responses/input_tokens` with timeout
  `deadline.timeout(10)`, no retry. `ClaimLost` propagates. Any other failure raises
  `ModelUnavailable("upstream_unavailable")`. Not ledgered as spend (the gate guarantees it is unbilled).
- `generate`, per attempt (the first attempt and the single retry follow the same steps):
  1. `reserve` (fenced) for `worst_case_usd`; `BudgetExceeded` → `ModelUnavailable("live_blocked")`.
  2. `checkpoints.hit("after_reserve")`.
  3. `await assert_claim(db, ctx.claim)` immediately before sending. On `ClaimLost`, nothing is sent. The
     reservation stays `reserved` for recovery (`recover_reservations` → `uncertain`), and `ClaimLost`
     propagates.
  4. Send with timeout `deadline.timeout(20)`, then `checkpoints.hit("after_send")`.
  5. Classify the outcome, then apply the ledger. With usage → `settle` (unfenced, so it applies even after
     `ClaimLost`). Otherwise → `release` or `mark_uncertain` inside `fenced`. If that raises `ClaimLost`, the row
     is left to recovery and `ClaimLost` propagates.
  6. Retry once only if `retryable`: a connect failure retries now, a rate limit after `rate_limit_delay`. The
     retry starts again at step 1 with a fresh reservation, so the claim is checked after any backoff sleep.
  7. Return a `ModelResponse` that keeps all `output` items, including reasoning.
- SDK call surface: use `client.responses.create(**body, timeout=…)`. For counting, use
  `client.responses.input_tokens.count(**body, timeout=…)` if the pinned SDK exposes it. Otherwise use
  `client.post("/responses/input_tokens", body=body, cast_to=httpx.Response)`. Record which one in the module
  docstring. Errors are read from `openai.APIStatusError.response`: `status_code`, `error.type`, `error.code`,
  `Retry-After`.

- [ ] **Step 1: Failing gate tests (AC 19)** (the Task 5 network guard is already active)

```python
async def test_disabled_live_mode_sends_zero_requests(openai_mock, scripted_app_journey):
    await scripted_app_journey()                              # full offline journey
    assert openai_mock.calls.call_count == 0
@pytest.mark.parametrize("override", ["live_disabled", "pricing_unverified", "token_count_billed", "reasoning_unverified"])
def test_gate_closed_fails_startup_without_client(override, openai_mock, monkeypatch):
    spy = spy_on(openai.AsyncOpenAI.__init__)
    with pytest.raises(LiveGateClosed): create_app(settings_with(override))
    assert spy.call_count == 0 and openai_mock.calls.call_count == 0
```

- [ ] **Step 2: Failing model tests (AC 16, 17, 20)** — all with `openai_mock` and the verified fixture configs

```python
async def test_count_and_generation_share_fields(model, openai_mock):          # AC 17
    await model.generate(...); count, gen = openai_mock.captured("input_tokens"), openai_mock.captured("responses")
    for k in ("model", "instructions", "input", "tools", "tool_choice"): assert count[k] == gen[k]
    assert "max_output_tokens" not in count and gen["store"] is False
async def test_count_failure_sends_no_generation(model, openai_mock): ...      # count 500 → ModelUnavailable; responses route 0 calls
async def test_count_time_charged_to_turn_deadline(model, openai_mock, fake_clock): ...
async def test_tool_call_round_trip_and_usage_settlement(core_live, openai_mock, support_db): ...   # AC 20
@pytest.mark.parametrize("scenario", ["400", "401", "403", "quota_429", "unclassified_429"])
async def test_no_retry_rows(model, openai_mock, scenario, support_db):        # AC 16
    assert openai_mock.route("responses").call_count == 1 and (await only_llm_call(support_db)).status == "uncertain"
async def test_transient_429_retry_within_deadline(model, openai_mock): ...    # Retry-After: 1 → 2 calls, 2 reservations
async def test_transient_429_beyond_deadline_not_retried(model, openai_mock): ...  # Retry-After: 60 → 1 call
async def test_connect_error_released_and_retried(model, openai_mock): ...     # httpx.ConnectError → released, 2nd call settles
async def test_timeout_uncertain_and_blocks(model, openai_mock, support_db): ...
async def test_unpriced_service_tier_in_response_blocks(model, openai_mock, support_db): ...
async def test_count_after_takeover_sends_nothing(model, openai_mock, support_db): ...   # take_over → ClaimLost, 0 calls
async def test_retry_after_takeover_not_sent(model, openai_mock, support_db): ...
    # connect error on attempt 1; take_over inside the mocked side effect → ClaimLost, responses call_count == 1

async def test_takeover_during_after_reserve_sends_no_generation(api_live_mocked, support_db, pause_at):   # AC 13
    gate = pause_at("after_reserve", turn=1)
    a = asyncio.create_task(api_live_mocked.send(mid := uuid4(), "minhas faturas")); await gate.reached.wait()
    await expire_lease(support_db, api_live_mocked.conversation_id)
    b = await api_live_mocked.send(uuid4(), "outra pergunta")          # B recovers A's turn
    gate.release(); a_result = await a
    assert a_result.json()["status"] == "interrupted"
    assert api_live_mocked.openai_routes["responses"].call_count == 0   # A never sent; B is live-blocked
    assert b.json()["reply"] == MESSAGES["live_blocked"]
    call = await only_llm_call(support_db, kind="generation")
    assert call.status == "uncertain" and call.reserved_usd > 0         # conservative: never released
```

The `after_reserve` test lives in `test_openai_model.py`. It needs `api_live_mocked`, so it runs after the
Task 17 fixtures exist.

- [ ] **Step 3: Failing test 18 end-to-end (AC 18)** — `test_history_truncation_e2e.py`

```python
@pytest.mark.parametrize("mode", ["none", "current_turn", "all_turns"])
async def test_truncation_boundary_with_mocked_provider(mode, live_app_factory, support_db, openai_mock):
    app = live_app_factory(reasoning_items=mode, max_input_tokens=1000)
    cid = await seed_history(support_db, turns=[
        plain_turn("T1"),
        tool_turn("T2", reasoning_by=["m", "other-model"], calls=["c1", "c2"]),
        interrupted_turn("T3", dangling_call="c3"),
        plain_turn("T4")])
    before = await items_by_id(support_db, cid)   # {id: full row tuple: turn_id, seq, kind, call_id, model, payload}
    openai_mock.count_returns([1200, 900])        # first count over limit → drop oldest selected turn → recount
    openai_mock.respond_text("Certo!")
    r = await send(app, cid, "e a minha conta?")
    assert r.json()["status"] == "completed"

    count_bodies = openai_mock.captured_all("input_tokens")
    assert len(count_bodies) == 2
    first_ids, second_ids = turn_markers(count_bodies[0]), turn_markers(count_bodies[1])
    assert second_ids == first_ids[1:]             # exactly the oldest whole turn dropped
    for body in count_bodies + openai_mock.captured_all("responses"):
        check_tool_pairs(body["input"])            # no orphans either way
        assert not any(i.get("call_id") == "c3" for i in body["input"])
        reasoning = [i for i in body["input"] if i.get("type") == "reasoning"]
        assert len(reasoning) == (1 if mode == "all_turns" and "T2" in turn_markers(body) else 0)
    after = await items_by_id(support_db, cid)    # filtering never mutates stored items:
    assert before.keys() <= after.keys()
    assert {k: after[k] for k in before} == before                    # every original row unchanged, by id
    assert {row.turn_id for k, row in after.items() if k not in before} == {r.json()["turn_id"]}   # new rows: this turn only

async def test_dropping_turn_with_tool_exchanges_removes_all_of_it(live_app_factory, support_db, openai_mock):
    # T2 is the oldest selected turn when the first count is over the limit
    ...
    assert {"c1", "c2"}.isdisjoint(call_ids(openai_mock.captured_all("responses")[0]["input"]))

async def test_still_over_after_one_drop_sends_no_generation(live_app_factory, support_db, openai_mock):
    openai_mock.count_returns([1200, 1100])
    r = await send(...)
    assert r.json()["reply"] == MESSAGES["limit_reached"] and openai_mock.route("responses").call_count == 0

async def test_current_turn_alone_too_large_sends_nothing(live_app_factory, openai_mock):
    r = await send(app, cid, "x" * 2000)            # with max_input_tokens=200
    assert r.json()["reply"] == MESSAGES["limit_reached"] and openai_mock.calls.call_count == 0
```

`turn_markers` reads a marker each seeded user message carries ("[T1]" … "[T4]"). `seed_history` writes turns and
`conversation_items` directly through the support schema.

- [ ] **Step 4: Run — expect FAIL**
  (`pytest tests/integration/test_openai_model.py tests/integration/test_history_truncation_e2e.py tests/integration/test_live_gate_app.py -v`)
- [ ] **Step 5: Implement `OpenAIResponsesModel` and the startup gate**
- [ ] **Step 6: Remove the `requires_openai_model` marks from Task 17. Run the whole support-api suite — expect
  PASS, 0 skipped** (`cd services/support_api && pytest -v`)
- [ ] **Step 7: Commit** — `feat(agent): OpenAI Responses model behind live gate (mocked HTTP only)`

---

### Task 19: Web UI

**Files:**
- Create: `web/{package.json,vite.config.js,index.html,playwright.config.js}`,
  `web/src/{main.jsx,App.jsx,api.js,ChatPanel.jsx,DebugDrawer.jsx,DemoDataPanel.jsx}`,
  `web/tests/{journey.spec.js,lockout.spec.js,errors.spec.js}`
- Modify: `docker-compose.yml` (add `web` on :5173, `VITE_API_TARGET=http://support-api:8000`)

**Interfaces:**
- Consumes: support-api HTTP API (Tasks 6, 17), `/api/health`, `/api/demo/customers`.
- Produces:
  - `vite.config.js`: proxy `/api` → `process.env.VITE_API_TARGET ?? "http://localhost:8000"` with
    `changeOrigin: true`. `server.allowedHosts`: `["localhost"]` plus `"." +
    process.env.GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN` when set. `host: true`.
  - `api.js`: `sendMessage(conversationId, text, clientMessageId)` retries network failures with the same id. On
    `status === "interrupted"` it returns `{retryWithNewId: true}`, and 409 maps to the `turn_in_progress` message
    from the body.
  - UI text: chat in PT-BR; offline badge from `/api/health` mode. The debug drawer (English) shows correlation id,
    tool calls (name, status, error code, attempts, duration, record count) and model calls (kind, model, tier,
    tokens, estimated cost, outcome class). The demo panel heading is "Dados de demonstração", labeled "dados
    sintéticos".

- [ ] **Step 1: Write failing Playwright tests** (run against the Compose stack)

```js
test("offline journey: bills → verify → summary", async ({ page }) => {          // AC 2
  await page.goto("/"); await expect(page.getByText("Modo offline — respostas simuladas")).toBeVisible();
  await send(page, "Quero ver minhas faturas");
  await expect(lastReply(page)).toContainText("CPF");
  await send(page, `${A.cpf_formatted}, ${A.birth_ddmmyyyy}, telefone ${A.last4}`);
  await expect(lastReply(page)).toContainText(A.expected_open_amount_brl);      // e.g. "R$ 187,42" from personas.json
});
test("three wrong attempts lock with identical generic messages", async ({ page }) => { ... });  // AC 5
test("409 shows the in-progress message, not a crash", async ({ page }) => {      // page.route returns 409
  await expect(lastReply(page)).toHaveText("Ainda estou processando sua mensagem anterior. Aguarde um momento.");
});
test("interrupted reply offers retry with a new id", async ({ page }) => { ... }); // page.route → status interrupted
test("debug drawer shows metadata only", async ({ page }) => { ... });           // no persona name/CPF inside drawer
```

- [ ] **Step 2: Run — expect FAIL** (`docker compose up -d --build && cd web && npx playwright test`)
- [ ] **Step 3: Implement components**
- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Commit** — `feat(web): PT-BR chat UI, debug drawer, demo data panel`

---

### Task 20: Full stack, docs, end-to-end acceptance

**Files:**
- Create: `scripts/smoke.sh`, `docs/troubleshooting.md`, `docs/provider-controls.md`,
  `services/support_api/tests/integration/test_e2e_faults.py`, `services/support_api/tests/integration/test_logs_redaction.py`,
  `services/support_api/tests/integration/test_lockout_sessions.py`
- Modify: `README.md`, `docker-compose.yml` (healthchecks, `depends_on: condition: service_healthy`; dev override
  `docker-compose.override.yml` sets `FAULT_INJECTION_ENABLED=true`)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write failing end-to-end tests** (support-api app + live `billing_server` with faults enabled)

```python
@pytest.mark.parametrize("effect,catalog_key,error_code", [
    ("timeout", "upstream_unavailable", "upstream_timeout"),
    ("http_500", "upstream_unavailable", "upstream_unavailable"),
    ("unavailable", "upstream_unavailable", "upstream_unavailable")])
async def test_billing_faults_recoverable(effect, catalog_key, error_code, verified_persona_e_api, set_fault, fake_clock):  # AC 9
    await set_fault("list_bills", "E", effect)
    r = await verified_persona_e_api.send(uuid4(), "minhas faturas")
    assert r.json()["reply"] == MESSAGES[catalog_key] and "R$" not in r.json()["reply"]
    assert r.json()["trace"]["tool_calls"][-1]["error_code"] == error_code and elapsed(fake_clock) <= 45

async def test_lockout_across_sessions_and_formats(app, billing_server): ...   # AC 6: wrong ×3 with "123.456.789-09" in session 1; session 2 with "12345678909" → locked, no verify call
async def test_concurrent_lockout_across_ten_sessions(app, billing_server, set_fault):   # AC 8, API level
    await set_fault("verify", None, "delay_ms:500")
    results = await asyncio.gather(*(fresh_session_wrong_attempt(app, A) for _ in range(10)))
    assert billing_server.verify_requests() <= 3 and all(r.reply == MESSAGES["auth_failed"] for r in results)
    assert await identifier_locked(A)
async def test_upstream_503_during_concurrent_verification_counts_nothing(app, billing_server, set_fault): ...   # AC 8 second half
async def test_logs_have_correlation_ids_and_no_persona_values(app, billing_server, caplog_json):  # AC 22
    await full_offline_journey(app)
    forbidden = persona_values()       # names, cpf, cpf_formatted, account_number, birth dates, phones, street, session token
    assert all(rec.get("correlation_id") for rec in caplog_json.records if rec["logger"].startswith("support_api"))
    assert not any(v in caplog_json.raw for v in forbidden)
```

- [ ] **Step 2: Run — expect FAIL**; **Step 3: implement** fixtures/fixes; **Step 4: run — expect PASS**
- [ ] **Step 5: Write docs.** The README covers fresh Codespace → `scripts/init-env.sh` → `docker compose up --build`
  → open port 5173, plus the offline badge and the demo data panel. `docs/troubleshooting.md` covers the
  `origin_not_allowed` symptom, the `allowedHosts` error, and the Secure cookie on `http://localhost` (works in
  Chromium/Firefox). `docs/provider-controls.md` covers OpenAI project hard monthly limit + alert (§5.6 last bullet),
  manual reconciliation of `uncertain` rows, and the live-gate prerequisites from spec §8, stated as not done.
- [ ] **Step 6: Commit** — `test: end-to-end acceptance; docs`

---

## Verification

Run these before declaring the milestone complete (superpowers:verification-before-completion). Each line lists the
command and what passing means.

| AC | Verified by | Command |
|---|---|---|
| 1 | Fresh-Codespace bring-up | `scripts/init-env.sh && docker compose up --build -d && scripts/smoke.sh` → all four health checks 200 within 120 s |
| 2, 5 | Playwright | `cd web && npx playwright test` → all pass |
| 3 | Task 3 + Task 10 tests | `pytest services/mock_billing -k foreign` and `pytest services/support_api -k "identifier_argument or foreign_bill"` |
| 4 | Task 6 | `pytest services/support_api -k other_session` |
| 6, 7, 8 | Tasks 9, 20 | `pytest services/support_api -k "lockout or admission or upstream_error"` |
| 9 | Task 20 | `pytest services/support_api -k billing_faults` |
| 10 | Task 1 | `pytest services/support_api -k denied_on_billing` |
| 11, 12, 13 | Tasks 17, 18 | `pytest services/support_api/tests/integration/test_turn_service.py services/support_api/tests/integration/test_recovery.py` and `pytest services/support_api -k "takeover or stale_worker"` |
| **14** | Tasks 9 + 17 | `pytest services/support_api -k "late_failure or late_verification"` → 4 tests pass |
| 15 | Task 12 | `pytest services/support_api -k concurrent_reservations` |
| 16 | Tasks 13, 18 | `pytest services/support_api -k "classify or retry or no_retry"` |
| 17 | Task 18 | `pytest services/support_api -k "share_fields or count_failure or count_time"` |
| **18** | Tasks 14 + 18 | `pytest services/support_api/tests/unit/test_history.py services/support_api/tests/integration/test_history_truncation_e2e.py` → all modes pass |
| 19 | Tasks 11, 18 | `pytest services/support_api -k "gate or zero_requests"` |
| 20 | Task 18 | `pytest services/support_api -k round_trip` |
| 21 | Task 4 | `pytest services/mock_billing -k faults` |
| 22 | Task 20 | `pytest services/support_api -k logs_have_correlation` |

Also run:

- **Full suites, no skips:** `pytest services/mock_billing services/support_api -v -rs` → 0 failed, 0 skipped.
- **Lint:** `ruff check services` → clean.
- **No-paid-calls check:**
  - `git grep -n '"verified": true' services/support_api/config` → no matches.
  - `grep -E '^(LIVE_LLM_ENABLED|OPENAI_API_KEY)=' .env.example` → `LIVE_LLM_ENABLED=false` and `OPENAI_API_KEY=`.
  - The network-guard fixture is autouse in `services/support_api/tests/conftest.py`.
- **Milestone report:** state that the OpenAI path is tested only against mocked HTTP and was not executed live
  (AC 20), and list the open §8 live-gate prerequisites.
