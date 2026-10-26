# Halcyon Logistics — hackathon dataset

This is **synthetic data** made for the hackathon. It describes a fictional logistics company, Halcyon Logistics Group. No real client, carrier, person or address appears in it, and nothing here should be treated as real business information.

## The situation

Halcyon moves goods for 280 enterprise clients from five warehouses, using six carriers. On-time delivery has fallen, clients are complaining and some are threatening to leave. Leadership believes the cause is carrier routing and selection, and is about to sign a contract for a routing optimisation product. Your task is in the Participant Problem Pack: work out what is really happening to Halcyon's deliveries, and tell leadership what to do about it, with evidence.

This data is what Halcyon has to work with. It covers about six months, from 1 April to the end of September 2026.

## Files

| File | Rows | What it holds |
|---|---|---|
| `deliveries.csv` | 48,340 | One row per delivery: the order, how and when it was handled, where it shipped from, which carrier took it and how it ended |
| `clients.csv` | 280 | One row per client |
| `carriers.csv` | 6 | The six carriers |
| `warehouses.csv` | 5 | The five warehouses |
| `warehouse_region_distance_miles.csv` | 5 | Approximate miles from each warehouse to each destination region |
| `sla_penalties.csv` | 4,847 | Service-level penalty payments |
| `complaints.csv` | 34 | Formal client complaints in the last 90 days |
| `customer_success_time_study.csv` | 18 | One week of hours for the customer success team |

`DATA_DICTIONARY.md` describes every column.

## How the files connect

- `deliveries.client_id` → `clients.client_id`
- `deliveries.carrier_id` → `carriers.carrier_id`
- `deliveries.nearest_warehouse_id` and `deliveries.fulfilment_warehouse_id` → `warehouses.warehouse_id`
- `sla_penalties.delivery_id` and `complaints.delivery_id` → `deliveries.delivery_id`

## Things to know

- The data is not clean. Some values are blank, and the columns are the raw records, not conclusions. Check the data before you rely on it.
- There is no column that says why a delivery was late. Working that out is part of the task.
- Dates are in ISO format (YYYY-MM-DD). Times are local.
- Use only this data. Do not look for or use real Halcyon, carrier or client information.
