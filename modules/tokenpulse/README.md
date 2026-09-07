# TokenPulse

AI **quota and budget** observability for AgeniusDesk: how close you are to a
spend limit, credit balances, and monthly budget gauges across Anthropic, OpenAI,
and OpenRouter, loudest first. The companion to the **Model Cost** module (which
breaks spend down per model) — this one answers "how close am I to a limit".

The module holds no credentials: every provider call goes through the host-owned
`http.request` bridge, so your admin keys stay host-side, in every isolation mode.

## What you get

- **Own view** (sidebar ▸ TokenPulse): quota gauges (budget vs actual, OpenRouter
  key limit, OpenRouter credit balance) sorted loudest first with reset
  countdowns, a per-provider spend strip, and a per-day monthly spend trend.
- **Main Dashboard card**: pin "AI Quotas" from **+ Widget** — the tightest quota
  up top, the next few below, with a link into the view.
- **Fleet Health row**: an "AI Quotas" row that turns amber past 75% and red past
  90% of the tightest quota.

## Quotas it shows

| Quota | Source | Notes |
|---|---|---|
| Monthly budget | Your budget vs actual MTD cost | Set a budget per provider in **Budgets**; the actual comes from the org cost API (Anthropic/OpenAI) or key usage (OpenRouter). |
| OpenRouter key limit | `/key` `limit` / `limit_remaining` | The spend cap on the key itself. |
| OpenRouter credits | `/credits` balance | Account credit balance; needs a provisioning/management key. |

## Setup

1. Install the module, then add the admin keys you have to the encrypted secret
   store and grant the matching endpoints (Settings ▸ Modules). Same keys as the
   Model Cost module; configure only the providers you use.

   | Provider | Default secret name | Key type |
   |---|---|---|
   | Anthropic | `ANTHROPIC_ADMIN_KEY` | Admin API key (`sk-ant-admin...`), read-only cost. |
   | OpenAI | `OPENAI_ADMIN_KEY` | Admin API key (`sk-admin-...`), read-only cost. |
   | OpenRouter | `OPENROUTER_KEY` | Key spend limit works with any key; the credit balance needs a **provisioning/management** key. |

   The default secret name is only a default — use **Configure endpoint ▸ Secret
   name** to point any endpoint at a different stored secret (e.g. give OpenRouter
   a management key under its own name). The module defines the auth shape; you
   choose which secret it resolves.

2. Keep the endpoints **read-only** (`GET` only) and confirm the base URLs.

3. Open TokenPulse, set a **monthly budget** per provider, and **Refresh**. Data
   is cached for up to 5 minutes.

## Endpoints (all read-only)

- Anthropic: `GET /v1/organizations/cost_report`
- OpenAI: `GET /v1/organization/costs`
- OpenRouter: `GET /key`, `GET /credits`

## Requirements

AgeniusDesk 0.6.0+ (http.request bridge, dashboard-card contribution API,
operator-selectable endpoint secret). Runs in every isolation mode; `container`
isolation is recommended for a credential-adjacent module. Inspired by the
standalone [TokenPulse](https://github.com/Mfrostbutter/tokenpulse) appliance.
