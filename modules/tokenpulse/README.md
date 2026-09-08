# TokenPulse

AI **cost and usage** observability for AgeniusDesk in one place: per-model spend,
monthly budget gauges, credit balances, and key limits across Anthropic, OpenAI,
and OpenRouter, with click-through provider detail and a per-project / per-key
cost breakdown.

The module holds no credentials: every provider call goes through the host-owned
`http.request` bridge, so your admin keys stay host-side, in every isolation mode.

## What you get

- **Own view** (sidebar ▸ TokenPulse): spend totals, quota gauges (budget vs
  actual, OpenRouter key limit and credit balance) loudest first, provider tiles,
  and a monthly spend trend. Click a provider tile to drill into its spend
  windows, quotas, cost-by-model table, daily trend, and breakdown.
- **Main Dashboard cards**: pin "AI Model Spend" and "AI Quotas" from **+ Widget**.
- **Fleet Health row**: an "AI Usage" row that turns amber past 75% and red past
  90% of the tightest quota.

## Per-project / per-key breakdown

Inside a provider's detail view, a **Breakdown** section splits that provider's
cost by tenant. A toggle picks the dimension; a sub-tile drills into that group's
cost-by-model table.

| Provider | Dimensions | Cost basis |
|---|---|---|
| Anthropic | Workspaces, API keys | Workspace total is **actual** (org cost API grouped by workspace); API-key total is **estimated** (tokens × price table). The per-model split is always estimated. |
| OpenAI | Projects, API keys | Project total is **actual** (org cost API grouped by project); API-key total is **estimated**. |
| OpenRouter | API keys | **Actual** lifetime usage vs each key's limit, enumerated with a provisioning/management key. |

You do **not** need to add a separate API key per tenant: the admin key's own
grouping produces the breakdown. Groups are a bonus — if a grouping call is not
permitted (or the endpoint is unavailable), the provider still renders and the
toggle is simply absent.

## Setup

1. Install the module, then add the admin keys you have to the encrypted secret
   store and grant the matching endpoints (Settings ▸ Modules). Configure only the
   providers you use.

   | Provider | Default secret name | Key type |
   |---|---|---|
   | Anthropic | `ANTHROPIC_ADMIN_KEY` | Admin API key (`sk-ant-admin...`), read-only cost + usage. |
   | OpenAI | `OPENAI_ADMIN_KEY` | Admin API key (`sk-admin-...`), read-only cost + usage. |
   | OpenRouter | `OPENROUTER_KEY` | Key spend limit works with any key; the credit balance, per-model spend, and per-key breakdown need a **provisioning/management** key. |

   The default secret name is only a default — use **Configure endpoint ▸ Secret
   name** to point any endpoint at a different stored secret (e.g. give OpenRouter
   a management key under its own name). The module defines the auth shape; you
   choose which secret it resolves.

2. Keep the endpoints **read-only** (`GET` only) and confirm the base URLs.

3. Open TokenPulse, set a **monthly budget** per provider, and **Refresh**. Data
   is cached for up to 5 minutes.

## Cost honesty

OpenRouter per-model spend (`/activity`) is **actual**. Anthropic and OpenAI
expose per-model tokens but total-only cost, so per-model cost there is derived
from tokens × the built-in price table and labelled **est**, with each provider's
actual total shown alongside. A model with no price on file shows **unpriced**.

## Endpoints (all read-only, `GET`)

- Anthropic: `/v1/organizations/cost_report`, `/v1/organizations/usage_report/messages`,
  `/v1/organizations/workspaces`, `/v1/organizations/api_keys`
- OpenAI: `/v1/organization/costs`, `/v1/organization/usage/completions`,
  `/v1/organization/projects`
- OpenRouter: `/key`, `/credits`, `/activity`, `/keys`

## Requirements

AgeniusDesk 0.6.0+ (http.request bridge, dashboard-card contribution API,
operator-selectable endpoint secret). Runs in every isolation mode; `container`
isolation is recommended for a credential-adjacent module. Inspired by the
standalone [TokenPulse](https://github.com/Mfrostbutter/tokenpulse) appliance.
