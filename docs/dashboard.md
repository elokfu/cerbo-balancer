# Dashboard

The controller page is `/dashboard/balancer`. The maintenance page is
`/dashboard/balancer-maintenance` and refreshes its telemetry snapshot every
five seconds while the RS485 poller runs every eight seconds. The controller
accepts a fresh complete telemetry sample for up to 20 seconds, allowing a
complete three-battery polling cycle and a bounded delayed retry without a
false stale-telemetry fault.

The maintenance page reads the latest parsed snapshot from
`/data/home/nodered/cerbo-balancer-latest.json`. Detailed parsed history is
retained for 24 hours, while the independent compact monthly summary is
recorded every 60 seconds for 30 days. Raw RS485 protocol data is not stored.

The dashboard reconnects automatically after a page reload or short network
interruption. The shared Dashboard configuration suppresses the transient
`Connection Lost` toast caused by the old page's socket closing during a
normal reload; it does not disable reconnect or telemetry safety handling.

The maintenance view exposes CID2 `0x61` system voltage/SOC and validated BMS
temperature range, CID2 `0x63` limits and permission/state bits, responding addresses, per-battery voltage and signed
current, per-battery and all-pack cell spread, cell voltages, voltage sums and
deltas, and every temperature sensor with min/max/average and interpretation.
Each battery also has a compact CID2 `0x44` status row showing MOSFET,
module-power, effective charge/discharge, protection, and alarm state. The
raw Status1–Status5 registers and complete protection, alarm, cell-fault, and
reserved-bit details are available under the collapsed `Status details`
section, keeping the normal view compact.
The CID2 `0x61` System Summary groups electrical values and SOH health,
cell extrema and IDs, cell temperatures, MOSFET temperatures, and BMS
temperatures. The health and capacity section adds average SOH, cycle count,
remaining capacity, and total capacity from the validated CID2 `0x42` tail.
IDs are shown beside valid measurements; trailing hex remains available only
in raw telemetry and logs.
All displayed CID2 `0x61` location IDs are decoded as `Battery N · Channel NN`
using the first byte as the channel/cell number and the second byte as the
battery number. Cell locations use `Cell NN`; temperature locations use
`Sensor NN`. The raw packed hexadecimal ID remains available in formatted
telemetry. The MOSFET temperature summary intentionally omits the average
row. If the CID2 `0x61` cell-temperature average is invalid, the page
calculates a fallback average from validated CID2 `0x42` sensor readings.
Unphysical CID2 `0x61` sentinel values such as `0xFFFF` temperatures are
displayed as `—` and are not used for control.
It also shows active addresses, pending-removal batteries, missed-scan counts,
the current 60-second or 10-second discovery schedule, serial ownership,
reconnect count, poll duration, and the latest classified communication error.

When the current poll is invalid but a valid sample is less than ten seconds
old, measurement cards remain visible with a `STALE` banner. This is display
only: the controller still treats the current telemetry as invalid and keeps
controller requests non-authoritative. After ten seconds, measurements are blanked.

The controller page reports the selected battery, addressed integer SOC and
extrema, state, completion latch, aggregate request, feed-forward share, PI
terms, and solar-limited pause counters. It controls selected-battery current
toward 2 A through aggregate DVCC current allowance without assuming equal
parallel current sharing.

`Automatic balancing` is the only balancing on/off control and defaults to ON
after a fresh/reset state or Restore Defaults. ON automatically selects the
first eligible battery. OFF releases selection, resets feed-forward and PI,
and holds `NORMAL` while requesting the Cerbo Charge Control voltage/current;
it does not disable charging. Reset Control clears controller terms without
changing the switch. Ordinary restarts preserve the persisted switch value.

The Cerbo battery-monitor menu is the only authority selector. The RS485
virtual BMS produces `APPLIED`; the CAN BMS produces `SHADOW`; missing
selection readback produces `UNKNOWN`. The controller never changes it.

The controller page also reports the active Cerbo BMS source and DeviceInstance
(`CAN Dyness BMS active`, instance `512`, or `RS485 virtual BMS active`,
instance `100`). Its Virtual BMS / DVCC panel separates requested values from
the final effective CVL, CCL, DCL, permissions, command freshness, thermal
factor, and arbitration reason. With the virtual BMS selected in Cerbo, the
standard effective values are the ones published to DVCC and visible through
the selected battery monitor in VRM.

The controller page includes live selected-current, selected Vmax/Vmin, and
selected-spread graphs independently of controller state. Its configuration
panel exposes the 30 mV spread threshold, current target, feed-forward filter
and gain, slow PI, aggregate bounds, solar tolerance, safety fallback, and
freshness limit. Unsaved edits are not overwritten by status refresh. Apply is
single-flight and waits for a matching controller acknowledgement; Discard
restores active values. Accepted settings are saved to
`cerbo-balancer-state.json` and the active configuration to
`cerbo-balancer-config.json`; both are restored on Node-RED startup.
They use append-only JSON snapshots; startup restores the final valid snapshot.

For PI-only characterization, use feed-forward gain 0.0, Kp 0.20, Ki 0.10,
integral limit 10 A, and 10 A/min upward slew. Reset control immediately before
the test. The 2 A startup request remains; subsequent feed-forward contribution
is zero. Restore feed-forward gain 1.0 and Ki 0.02 afterward.
