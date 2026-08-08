# Dashboard

The controller page is `/dashboard/balancer`. The maintenance page is
`/dashboard/balancer-maintenance` and refreshes its telemetry snapshot every
five seconds while the RS485 poller runs every two seconds.

The maintenance view exposes CID2 `0x61` system voltage/SOC and validated BMS
temperature range, CID2 `0x63` limits and permission/state bits, responding addresses, per-battery voltage and signed
current, per-battery and all-pack cell spread, cell voltages, voltage sums and
deltas, and every temperature sensor with min/max/average and interpretation.
Each battery also has a compact CID2 `0x44` status row showing MOSFET,
module-power, effective charge/discharge, protection, and alarm state. The
raw Status1–Status5 registers and complete protection, alarm, cell-fault, and
reserved-bit details are available under the collapsed `Status details`
section, keeping the normal view compact.
The CID2 `0x61` System Summary groups electrical values, cycle/SOH health,
cell extrema and IDs, cell temperatures, MOSFET temperatures, and BMS
temperatures. IDs are shown beside valid measurements; trailing hex remains
available only in raw telemetry and logs.
CID2 `0x61` cell-location IDs are displayed as `Battery N · Cell NN` using
the first byte as the cell number and the second byte as the battery number;
the raw packed hexadecimal ID remains available in the formatted telemetry.
Unphysical CID2 `0x61` sentinel values such as `0xFFFF` temperatures are
displayed as `—` and are not used for control.
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
