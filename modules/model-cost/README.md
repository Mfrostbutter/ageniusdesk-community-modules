# Model Cost

Per-model AI cost observability for AgeniusDesk. See which **models** are actually
costing you, with tokens and spend broken out by model, across Anthropic, OpenAI,
and OpenRouter, in the module's own view and as a pinnable card on the Main
Dashboard.

The module holds no credentials and opens no direct connection: every provider
call goes through the host-owned `http.request` bridge, so your admin keys stay
host-side and never enter module code, in every isolation mode.

## What you get

- **Own view** (sidebar ▸ Model Cost): a per-provider strip and a per-model table
  you can tab by window (month to date, today, and OpenRouter's 30-day activity),
  sorted by cost, with tokens in/out per model.
- **Main Dashboard card**: pin "AI Model Spend" from **+ Widget**. Today's and the
  month's actual spend plus your top models by cost, with a link into the view.
- **Fleet Health row**: an "AI Spend" row in the Fleet Health pane.

## Cost honesty

Provider **cost** endpoints report a total, not a per-model breakdown; their
**usage** endpoints break down by model (tokens only). So:

- **OpenRouter** reports real per-model spend (last 30 completed UTC days). Shown
  as **actual**.
- **Anthropic** and **OpenAI** per-model cost is **estimated** from token counts
  times a built-in price table (`prices.py`), and always labelled. Each provider's
  **actual** total (from its cost endpoint) is shown alongside so any drift between
  the estimate and the real bill is visible, never hidden. An unpriced model shows
  its tokens with the cost marked `unpriced` rather than a false zero.

Prices drift; edit `prices.py` to keep the estimate honest for your models.

## Setup

1. Install the module (Settings ▸ Modules ▸ Install), then add the admin keys you
   have to the encrypted secret store and grant the matching endpoints. Configure
   only the providers you use; the rest simply show as "not configured".

   | Provider | Secret | Key type |
   |---|---|---|
   | Anthropic | `ANTHROPIC_ADMIN_KEY` | Admin API key (`sk-ant-admin...`). A plain `sk-ant-api` key cannot read org usage/cost. |
   | OpenAI | `OPENAI_ADMIN_KEY` | Admin API key (`sk-admin-...`). |
   | OpenRouter | `OPENROUTER_KEY` | A provisioning/management key returns 30-day spend by model; an inference key returns only key-level limits. |

2. In the endpoint consent panel, confirm the base URLs and keep the endpoints
   **read-only** (only `GET` is needed). The keys are read-only usage/cost
   credentials; this module never writes to a provider.

3. Open Model Cost and **Refresh**. Data is cached for up to 5 minutes.

## Endpoints (all read-only)

- Anthropic: `GET /v1/organizations/cost_report`, `GET /v1/organizations/usage_report/messages`
- OpenAI: `GET /v1/organization/costs`, `GET /v1/organization/usage/completions`
- OpenRouter: `GET /activity`, `GET /credits`, `GET /key`

## Requirements

AgeniusDesk 0.6.0+ (needs the `http.request` host bridge and the dashboard-card
contribution API). Runs in every isolation mode; `container` isolation is
recommended for a credential-adjacent module.
