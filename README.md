# Halcyon OTD Control Tower (Team CSC BLP · FDE AI Hackathon 5.0)

Halcyon thinks routing is why deliveries are late. The data says otherwise. This app shows why, with evidence, and lets leadership test any plan before signing the $680K routing contract.

**Slice 1 (this commit): analytics + dashboard.** Slice 2 adds the Claude agents (email intake, delay alerts, evidence Q&A) from the solution playbook.

## What it shows

| Tab | Finding |
| --- | --- |
| Overview | OTD is 71.0% and flat. Orders with none of the four upstream problems are already 92.2% on time. |
| Root causes | Four drivers (address, same-day cutoff, stock, carrier lane) explain 85% of late deliveries. Routing has no effect (odds ratio 1.00 per stop, p = 0.76). |
| Carrier lanes | D, E and F each fail on specific regions (43–46% late on clean orders, against about 5%). The output is a despatch rules table. |
| Stock placement | 12% of orders ship from a far warehouse (875 vs 125 miles median) because the nearest one had no stock. |
| Accounts at risk | The 9 accounts ($14.2M) threatening to leave, with each one's biggest driver. |
| What-if | Sliders for each fix and for the vendor's routing cut, giving the expected OTD. |

## Design principles

- **Numbers come from code, never from an LLM.** `backend/app/analytics.py` holds pandas plus a logistic regression (statsmodels) that controls for commitment type, channel, stops and route km.
- **Rules are learned from data.** A bad lane is one whose late rate on clean orders is at least 2× the network rate. Thresholds are set through `OTD_*` environment variables.
- **Data is checked before it is trusted.** The checks are shown on the Overview tab.
- **Tests pin the claims.** `backend/tests` asserts the sponsor's numbers (48,340 deliveries, 71% OTD, $14.2M, 34 complaints) and that routing has no effect.

## Layout

```text
backend/    FastAPI + analytics, Dockerfile, tests, data/ (synthetic hackathon CSVs)
frontend/   React + TypeScript (Vite) + Recharts, nginx Dockerfile
infra/      Bicep for Azure Container Apps (deploys into rg-csc-blp)
azure.yaml  azd project (azd up)
docs/       DEPLOY.md
```

## API

`GET /api/overview`, `/api/drivers`, `/api/bridge`, `/api/lanes`, `/api/stock`, `/api/accounts`, `/api/deliveries`, `POST /api/simulate`. Interactive docs are at `/docs`.

Deploy: see [docs/DEPLOY.md](docs/DEPLOY.md).
