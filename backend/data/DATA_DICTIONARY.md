# Data dictionary

All files are CSV with a header row. All values are synthetic.

## deliveries.csv

| Column | Meaning |
|---|---|
| `delivery_id` | Unique delivery, in the order orders were received |
| `order_id` | Order reference |
| `client_id` | The client who placed the order |
| `order_channel` | How the order reached Halcyon: `EDI`, `API` or `Email`. Email orders are keyed into the warehouse system by customer success staff |
| `email_format_id` | For email orders, which of 14 layouts clients use (1 to 14); blank otherwise |
| `order_received_ts` | When the order was received (`YYYY-MM-DD HH:MM`) |
| `commitment_type` | The delivery promise class for the order: `same_day`, `next_day` or `standard` |
| `promised_date` | The date delivery was promised |
| `address_status` | The state of the delivery address when the order was ingested: `complete` or `incomplete` |
| `pick_list_date` | The date of the pick list the order was placed on |
| `shipped_ts` | When the carrier collected the shipment |
| `destination_region` | Where the delivery was going: `Midwest`, `Southeast`, `Northeast` or `West` |
| `nearest_warehouse_id` | The warehouse closest to the destination |
| `fulfilment_warehouse_id` | The warehouse the order was actually fulfilled from |
| `nearest_warehouse_had_stock` | `Y` if the nearest warehouse held the stock when the order was fulfilled, `N` if not |
| `distance_miles` | Approximate distance from the fulfilling warehouse to the destination |
| `carrier_id` | The carrier that took the shipment |
| `carrier_rate_usd` | The rate charged for the shipment |
| `stops_on_route` | Number of stops on the delivery route |
| `planned_route_km` | Planned length of the delivery route |
| `actual_route_km` | Actual length driven; blank if not recorded |
| `delivered_date` | The date the shipment was delivered |
| `days_late` | `delivered_date` minus `promised_date` in days. Negative means early |
| `on_time` | `Y` if delivered on or before the promised date, otherwise `N` |
| `redelivery_cost_usd` | Cost of sending the shipment again; 0 if it was not needed |

## clients.csv

| Column | Meaning |
|---|---|
| `client_id` | Unique client |
| `client_name` | A placeholder name |
| `region` | The client's home region |
| `primary_order_channel` | The channel the client mainly uses |
| `account_tier` | `Tier 1`, `Tier 2` or `Tier 3`, by annual revenue |
| `sla_penalty_clause` | `Y` if the contract carries a service-level penalty clause |
| `annual_revenue_usd` | Annual revenue Halcyon earns from the client |
| `account_status` | `active`, or `threatening_to_leave` |

## carriers.csv

| Column | Meaning |
|---|---|
| `carrier_id` | Unique carrier (A to F) |
| `carrier_name` | Name |
| `has_api` | `Y` if the carrier offers an API for delivery events |
| `delivery_event_feed` | How Halcyon receives the carrier's delivery events |
| `preferred_supplier_agreement` | `Y` if Halcyon has a preferred-supplier agreement with the carrier |
| `base_rate_usd` | Base rate per shipment |

## warehouses.csv

| Column | Meaning |
|---|---|
| `warehouse_id` | Unique warehouse (W1 to W5) |
| `warehouse_name` | Name |
| `region` | The region the warehouse serves |
| `pick_list_cutoff` | The time each morning the day's pick list is compiled |
| `carrier_collection_window` | The window in which carriers collect |

## warehouse_region_distance_miles.csv

One row per warehouse and one column per destination region: approximate miles between them.

## sla_penalties.csv

| Column | Meaning |
|---|---|
| `penalty_id` | Unique penalty |
| `delivery_id` | The delivery that breached the service level |
| `client_id` | The client paid the penalty |
| `days_late` | How late the delivery was |
| `penalty_usd` | The penalty paid |

## complaints.csv

| Column | Meaning |
|---|---|
| `complaint_id` | Unique complaint |
| `complaint_date` | Date the complaint was logged |
| `client_id` | The client |
| `delivery_id` | The delivery complained about |
| `channel` | How the client made the complaint |
| `category` | The client's stated reason |
| `escalated_to_account_owner` | `Y` if the complaint was escalated |
| `text` | A short note of what the client said |

## customer_success_time_study.csv

| Column | Meaning |
|---|---|
| `staff_id` | A customer success team member |
| `week_of` | The week studied |
| `hours_total` | Hours worked that week |
| `hours_on_delivery_complaints` | Hours spent handling delivery complaints and escalations |
| `hours_keying_email_orders` | Hours spent keying email orders into the warehouse system |
| `hours_on_proactive_client_work` | Hours spent on proactive client work |
