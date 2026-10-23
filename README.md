# Utility agentic support

A PT-BR chat demonstration for **Vértice Energia**, a fictional utility. By default it runs offline: a scripted model calls a real support API, synthetic billing API, and Postgres, and no OpenAI key is needed. [Live mode](#live-mode-openai) swaps in OpenAI `gpt-6-luna` behind a budget-capped gate.

## Start in a fresh Codespace

Docker with Compose v2, Bash, OpenSSL, and curl must be available. From the repository root:

```sh
scripts/init-env.sh
docker compose up --build -d
scripts/smoke.sh
```

After a Codespace restart, `scripts/demo-up.sh` does all of this in one step: it checks container networking (see [troubleshooting](docs/troubleshooting.md#containers-cannot-reach-the-database-after-a-codespace-restart)), rebuilds, starts, and runs smoke.

Open **port 5173** from the Codespaces Ports panel. Locally, open <http://localhost:5173>. `init-env.sh` generates random local secrets and the exact browser origin in the ignored `.env`; it refuses to overwrite an existing file. On later starts, retain `.env` and run only the last two commands. The first image build depends on network access for dependencies; smoke waits at most 120 seconds after startup for all four services. It checks native Postgres readiness and HTTP 200 for the other three services.

The badge **“Modo offline — respostas simuladas”** confirms scripted mode. Open **“Dados de demonstração”** for the five clearly labeled synthetic personas and their verification answers. Ask “minhas faturas”, then enter one persona's CPF/account, birth date, and last four phone digits. All identifiers and bills are synthetic; generated CPFs have valid check digits solely for fixtures. The English debug drawer shows IDs, timing, error codes and usage metadata.

## Services and fault demos

| Service | Host endpoint | Persistence |
| --- | --- | --- |
| Web/Vite | `http://localhost:5173/` | Conversation ID in browser localStorage |
| Support API | `http://localhost:8000/api/health` | `support` schema |
| Mock billing | `http://localhost:8001/health` | `billing` schema |
| Postgres 16 | `127.0.0.1:5432` (native readiness) | Named `pgdata` volume |

The browser uses the web origin and same-origin `/api` proxy. The two API ports bind to loopback. Each service migrates its own schema; `support_app` cannot read billing tables. Existing database volumes are retained on rebuilds. Initialization SQL runs only for an empty database volume.

Compose automatically loads `docker-compose.override.yml`, enabling authenticated dev fault rules on mock billing. `PUT /_dev/faults` replaces rules and `DELETE /_dev/faults` clears them; both require the separate `X-Fault-Admin-Key` from the ignored `.env`. The support API never receives that admin key. Rule shape:

```json
{"rules": [{"route_name": "list_bills", "customer_ref": "<demo customer_ref>", "effect": "unavailable"}]}
```

Supported route names: `verify`, `account`, `list_bills`, `bill_detail`. Effects: `timeout`, `http_500`, `unavailable` (503), `delay_ms:500`. Omit `customer_ref` for a whole route; verification has no customer path and requires an unscoped rule. Rules are process-local and first-match. To run the base configuration with fault administration disabled, explicitly use `docker compose -f docker-compose.yml up --build -d`.

## Live mode (OpenAI)

Live mode sends real, paid requests. Before switching it on:

1. In the OpenAI dashboard, use a dedicated project with a **hard monthly spend limit** and an alert (see [provider controls](docs/provider-controls.md)). The app's own ledger (`PROJECT_BUDGET_USD`, default 20; `CONVERSATION_BUDGET_USD`, default 0.10) is an estimate, not a guaranteed cap.
2. Create an API key in that project and add it to `.env` yourself, by editing the file: `OPENAI_API_KEY=sk-...` (no quotes). Never paste it into chat, issues or commits.
3. Switch modes; the script rebuilds and restarts only the support API:

```sh
scripts/set-mode.sh live      # LLM_PROVIDER=openai, LIVE_LLM_ENABLED=true
scripts/set-mode.sh offline   # back to the scripted model at any time
```

If live startup fails, the script switches back to offline automatically and points to `docker compose logs support-api`. In live mode the offline badge disappears and the debug drawer shows each token count and generation with its tokens and estimated cost. Optional `.env` settings: `OPENAI_CHAT_MODEL` (default `gpt-6-luna`; other models need verified entries in `services/support_api/config/`), `OPENAI_REASONING_EFFORT` (default `low`; `none`/`low`/`medium` suit the 800-token output cap), and the two budget variables. [Provider controls](docs/provider-controls.md) explains how to watch spend and reconcile uncertain rows.

## Verify changes

Use Python **3.12** (the Codespace's default `python3` may differ):

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e 'services/mock_billing[dev]' -e 'services/support_api[dev]'
.venv/bin/pytest --asyncio-mode=auto --tb=short services/mock_billing services/support_api -v -rs
.venv/bin/ruff check services
.venv/bin/ruff format --check services
cd web
npm ci
npx playwright install --with-deps chromium
npm run build
npx playwright test --workers=2
```

The backend suites rebuild only the dedicated `uas_test` schemas and use a fault-enabled billing subprocess. Run the combined command sequentially: an independent mock-billing schema test process must not overlap the support API's billing fixture. Every support API test has an autouse network guard against real OpenAI connections. OpenAI integration tests use mocked HTTP and fake keys. Root pytest needs the explicit `--asyncio-mode=auto` flag because service-local pytest settings do not apply to the combined root invocation. The two ruff commands above, run from the repository root, are the canonical lint and format gate; running ruff from inside a service directory classifies first-party imports differently and reports import-order (`I001`) findings that the root run does not.

For Playwright inside a Codespace, localhost must also be permitted. Apply this additive runtime-only override before running the browser suite; it keeps the generated Codespaces origin and leaves `.env` unchanged:

```sh
.venv/bin/python - <<'PY'
import os
from pathlib import Path
import subprocess
origin = next(line.split('=', 1)[1]
              for line in Path('.env').read_text().splitlines()
              if line.startswith('ALLOWED_ORIGINS='))
env = dict(os.environ, ALLOWED_ORIGINS=origin + ',http://localhost:5173')
subprocess.run(['docker', 'compose', 'up', '-d', '--build'], env=env, check=True)
PY
```

[Acceptance evidence](docs/milestone-1-report.md), [troubleshooting](docs/troubleshooting.md), and [provider controls and live prerequisites](docs/provider-controls.md) describe operational limits. Logs and the drawer exclude payloads, but the database retains raw synthetic messages, tool arguments/results, and opaque reasoning for history replay. A live model would receive the knowledge answers typed in chat. Keep this demo restricted to synthetic data.
