# Dashboard

The controller page is `/dashboard/balancer`. The maintenance page is
`/dashboard/balancer-maintenance` and refreshes its telemetry snapshot every
five seconds while the RS485 poller runs every two seconds.

The maintenance view exposes CID2 `0x61` system voltage/SOC, CID2 `0x63`
limits and status, responding addresses, per-battery voltage and signed
current, all effective cells with reported/calculated source, voltage sums and
deltas, and every temperature sensor with min/max/average and interpretation.

Mode starts at `TEST`. TEST calculates shadow pack-voltage/current commands but
performs no charger or voltage write. ACTIVE is rejected unless fresh,
independently validated per-battery cell telemetry, valid configuration, PI
gains, and verified output readback are present.

Mode starts at `TEST`. The ACTIVE control is intentionally rejected unless
cell telemetry and output readback are verified. TEST commands are diagnostic
intent only; no physical charger or voltage setting is changed.
