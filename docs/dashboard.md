# Dashboard

The controller page is `/dashboard/balancer`. The maintenance page is
`/dashboard/balancer-maintenance` and refreshes its telemetry snapshot every
five seconds while the RS485 poller runs every two seconds.

The maintenance view exposes CID2 `0x61` system voltage/SOC, CID2 `0x63`
limits and status, responding addresses, per-battery voltage and signed
current, per-battery and all-pack cell spread, cell voltages, voltage sums and
deltas, and every temperature sensor with min/max/average and interpretation.
It also shows active addresses, pending-removal batteries, missed-scan counts,
the current 60-second or 10-second discovery schedule, serial ownership,
reconnect count, poll duration, and the latest classified communication error.

When the current poll is invalid but a valid sample is less than ten seconds
old, measurement cards remain visible with a `STALE` banner. This is display
only: the controller still treats the current telemetry as invalid and keeps
ACTIVE blocked. After ten seconds, measurements are blanked.

Mode starts at `TEST`. TEST calculates shadow pack-voltage/current commands but
performs no charger or voltage write. ACTIVE is rejected unless fresh,
independently validated per-battery cell telemetry, valid configuration, PI
gains, and verified output readback are present.

Mode starts at `TEST`. The ACTIVE control is intentionally rejected unless
cell telemetry and output readback are verified. TEST commands are diagnostic
intent only; no physical charger or voltage setting is changed.
