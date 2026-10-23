# Provider controls and the live gate

The demo runs offline by default: `LLM_PROVIDER=scripted`, `LIVE_LLM_ENABLED=false` and an empty `OPENAI_API_KEY`. Live generation with OpenAI `gpt-6-luna` is available behind the gate (design §5.6, rev 5). The shipped `config/pricing.json` and `config/models.json` record official sources checked on 2026-10-23, so the gate opens only when the owner sets all three switches in `.env`. See [the README](../README.md#live-mode-openai) for the steps.

## What the shipped config records

| Item | Value | Source |
|---|---|---|
| `gpt-6-luna` default tier | $0.10 input, $0.01 cached input, $0.50 output per 1M tokens | [OpenAI pricing](https://developers.openai.com/api/docs/pricing) |
| `gpt-6-luna` flex tier | $0.05 / $0.005 / $0.25 per 1M tokens (not reachable through Compose, which leaves `OPENAI_SERVICE_TIER` at `default`) | same |
| Token counting | `billed: true`, an assumption: OpenAI does not document `POST /v1/responses/input_tokens` as free | [Counting tokens guide](https://developers.openai.com/api/docs/guides/token-counting) |
| Reasoning replay | `current_turn`: GPT-6 Luna is not listed as supporting `all_turns` | [Reasoning guide](https://developers.openai.com/api/docs/guides/reasoning) |

Because counting is recorded as billed, every count has its own `llm_calls` row (`kind = 'token_count'`), reserved before sending and settled at the input rate. Billed counts add roughly 40% to an input-heavy call. A typical demo turn costs about $0.001–0.002, so the default $0.10 conversation cap covers a normal conversation; long, tool-heavy conversations can reach it before the 30-turn limit.

## Provider-side monthly controls

Before live use, use a dedicated OpenAI project and configure both a **hard monthly project spend limit** (at or below the approved budget) and a lower **alert threshold**. The official [OpenAI spend-limits guide](https://developers.openai.com/api/docs/guides/spend-limits) documents project Settings → Limits → Spend → Edit spend limit → Monthly spend limit → Enforce a hard limit → Save. A reached hard project limit rejects applicable requests with HTTP 429 `project_spend_limit_exceeded`; organization limits use `organization_spend_limit_exceeded`. Enforcement can lag and recorded spend can slightly exceed the configured amount. Alerts are notification-only: traffic continues. These controls reset monthly and cannot impose a cumulative multi-month cap.

The application's Postgres ledger is an independent estimate using model/tier token prices. Reservations serialize against the project cap (`PROJECT_BUDGET_USD`, default 20, written to the database at every startup) and the conversation cap (`CONVERSATION_BUDGET_USD`, default 0.10; a change applies to new conversations only). The ledger is not an authoritative provider bill or a guaranteed spending cap: prices are entered by hand, settlement records actual usage even when it exceeds the hold, the ledger restarts if the `pgdata` volume is recreated, and spend outside this database is invisible to it. Keep `PROJECT_BUDGET_USD` a little below the provider hard limit. Retry eligibility and charge certainty are separate: an error does not prove a request was free.

## Watch spend

With live mode on, read the ledger without exporting messages:

```sh
docker compose exec -T db psql -U postgres -d uas -c \
  "select kind, status, count(*), sum(estimated_usd) as settled_usd, sum(reserved_usd) as held_usd
   from support.llm_calls group by 1, 2 order by 1, 2"
docker compose exec -T db psql -U postgres -d uas -c \
  "select cap_usd, spent_usd, reserved_usd, uncertain_usd from support.budget_accounts"
```

Compare `spent_usd` with the OpenAI usage dashboard after the first few turns. A large gap means a price or the counting assumption is wrong: switch back with `scripts/set-mode.sh offline` and correct `pricing.json`.

## Manually reconcile `uncertain` rows

An ambiguous outcome keeps the full reservation in `uncertain`. Never automatically release a possibly charged reservation or replace unknown usage with zero. There are two kinds:

- **Generation, or a call cut off by a crash:** the conversation is also blocked from live generation (`live_blocked_reason`), while scripted mode remains usable.
- **Token counts that failed transiently** (5xx, timeout, rate limit, malformed result): the hold stays `uncertain` but the conversation is **not** blocked, so the next turn counts again. Find them with `kind = 'token_count' AND status = 'uncertain'`. Each is at most a few tenths of a cent. Quota and 4xx count failures do block, like generation.

Ledger writes that fail after a provider response (for example, the database going away mid-turn) can also leave a row `reserved` after its turn has ended. Treat `reserved` rows of finished turns like `uncertain` ones.

An authorized operator must reconcile deliberately, with live mode still off:

1. Identify the affected `llm_calls` rows by conversation/turn ID, kind, model, service tier, attempt, timestamp, and provider request ID when present. Inspect associated project/conversation budget counters and `live_blocked_reason`. Do not export messages, knowledge answers, opaque reasoning, or keys.
2. Compare with authoritative provider usage/billing evidence. A missing provider record or a local timeout alone is insufficient evidence that nothing was billed. Retain `uncertain` (and any block) while the evidence remains ambiguous.
3. If actual usage is recovered, use the existing idempotent settlement transition for that row: `uncertain → settled`, charge the reported usage using verified model/tier pricing, and move the same reservation out of uncertain totals for both budget accounts. A stale worker that later receives actual usage can perform this own-row settlement too.
4. Only with authoritative evidence of no charge should an operator perform a reviewed transactional reconciliation to release that reservation and adjust both budget totals consistently. There is no automatic uncertain-release endpoint or ready-made operator CLI. `release()` is intentionally limited to `reserved` rows proven unsent; it must not be repurposed as an uncertain-release shortcut. The CHECK constraints from migration 0003 reject any adjustment that would make an amount negative.
5. Keep an audit record of evidence, row IDs, amounts, and before/after counters. Clear a conversation's live block only after all its uncertain rows are resolved and the independent cause is understood, in a reviewed transaction. Reconciliation never itself enables live generation.

## Still open

- Token-counting rate limits are undocumented.
- Strict-schema support for `gpt-6-luna` is unconfirmed (non-blocking).
- The reasoning-replay setting is inferred from the guide; the first live calls should confirm it by comparing counts with and without an earlier turn's reasoning items.

## Fencing limit

The rev 4 stale-worker guarantee applies to pauses before a pre-send gate, including `after_model_response`, `after_reserve`, `after_count_reserve`, `after_verify_admit`, and `after_claim_check` followed by an expired local deadline. It does not close a whole-process stall of at least 15 seconds after the local check passes and before a request completes, nor an equivalent database-clock jump. At most one stale request per worker can escape; a reservation becomes uncertain and blocks live mode, billing GET results are discarded by fenced writes, and late verification outcomes count failures without granting assurance. Receiver-side fencing would be needed to close this race.

Logs and the debug drawer allow metadata only. The database still stores raw synthetic message/tool payloads and opaque reasoning for replay; a live model sees the knowledge answers typed in chat. Keep real customer data out of this demo.
