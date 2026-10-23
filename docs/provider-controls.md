# Provider controls and the closed live gate

Milestone 1 is a synthetic offline demonstration. `LLM_PROVIDER=scripted`, `LIVE_LLM_ENABLED=false`, and an empty `OPENAI_API_KEY` are sufficient. Shipped `config/pricing.json` and `config/models.json` intentionally keep `verified: false`. OpenAI paths have been tested only against mocked HTTP; no live request, paid call, or real key was used. No provider-account configuration was changed.

## Provider-side monthly controls

Before any separately approved live use, use a dedicated OpenAI project and configure both a **hard monthly project spend limit** and a lower **alert threshold**. The official [OpenAI spend-limits guide](https://developers.openai.com/api/docs/guides/spend-limits) documents project Settings → Limits → Spend → Edit spend limit → Monthly spend limit → Enforce a hard limit → Save. A reached hard project limit rejects applicable requests with HTTP 429 `project_spend_limit_exceeded`; organization limits use `organization_spend_limit_exceeded`. Enforcement can lag and recorded spend can slightly exceed the configured amount. Alerts are notification-only: traffic continues. These controls reset monthly and cannot impose a cumulative multi-month cap.

The application's Postgres ledger is an independent estimate using model/tier token prices. Reservations serialize against project and conversation caps (defaults USD 20 and USD 0.10). The ledger is not an authoritative provider bill or a guaranteed spending cap. Retry eligibility and charge certainty are separate: an error does not prove a request was free.

## Manually reconcile `uncertain` rows

An ambiguous generation outcome keeps the full reservation in `uncertain`, including a crash after a request may have been sent. The conversation is blocked from live generation, while scripted mode remains usable. Never automatically release a possibly charged reservation or replace unknown usage with zero.

An authorized operator must reconcile deliberately, with live mode still off:

1. Identify the affected `llm_calls` rows by conversation/turn ID, model, service tier, attempt, timestamp, and provider request ID when present. Inspect associated project/conversation budget counters and `live_blocked_reason`. Do not export messages, knowledge answers, opaque reasoning, or keys.
2. Compare with authoritative provider usage/billing evidence. A missing provider record or a local timeout alone is insufficient evidence that nothing was billed. Retain `uncertain` and the block while the evidence remains ambiguous.
3. If actual usage is recovered, use the existing idempotent settlement transition for that row: `uncertain → settled`, charge the reported usage using verified model/tier pricing, and move the same reservation out of uncertain totals for both budget accounts. A stale worker that later receives actual usage can perform this own-row settlement too.
4. Only with authoritative evidence of no charge should an operator perform a reviewed transactional reconciliation to release that reservation and adjust both budget totals consistently. The milestone provides no automatic uncertain-release endpoint or ready-made operator CLI. `release()` is intentionally limited to `reserved` rows proven unsent; it must not be repurposed as an uncertain-release shortcut.
5. Keep an audit record of evidence, row IDs, amounts, and before/after counters. Clear the conversation's live block only after all uncertain rows are resolved and the independent cause is understood, in a reviewed transaction. Retain the block for any remaining uncertainty. Reconciliation never itself enables live generation.

## Open prerequisites — not completed

The following design §8 prerequisites remain **unverified and not done**. They are deliberately blocking live generation, while mocked milestone acceptance can proceed:

- Verify official pricing for the selected model **and processing tier**; record the source URL and verification date in `pricing.json`.
- Obtain official confirmation that `POST /v1/responses/input_tokens` is unbilled and document its rate limits. The token-counting guide does not establish this. If counting is billed, design a reservation path for count requests before enabling live generation.
- Confirm the selected model's reasoning replay behavior (`none`, `current_turn`, or `all_turns`) and record it in `models.json`. Stateless encrypted reasoning replay exists; the model-specific setting remains unverified.

Any live execution also needs explicit spending approval. Do not turn the shipped placeholders into verified entries solely to bypass the gate. Non-blocking future work includes confirming strict-schema support for the selected model before relying on it and the later project handoff.

## Fencing limit

The rev 4 stale-worker guarantee applies to pauses before a pre-send gate, including `after_model_response`, `after_reserve`, `after_verify_admit`, and `after_claim_check` followed by an expired local deadline. It does not close a whole-process stall of at least 15 seconds after the local check passes and before a request completes, nor an equivalent database-clock jump. At most one stale request per worker can escape; a generation reservation becomes uncertain and blocks live mode, billing GET results are discarded by fenced writes, and late verification outcomes count failures without granting assurance. Receiver-side fencing would be needed to close this race.

Logs and the debug drawer allow metadata only. The database still stores raw synthetic message/tool payloads and opaque reasoning for replay; a live model would see typed knowledge answers. Keep real customer data out of this milestone.
