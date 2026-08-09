# Cerbo Dyness Balancer

Dyness/Pylon-compatible RS485 telemetry and a TEST-mode active-balancing
controller for Cerbo GX. The RS485 adapter is polled at 115200 8N1 using only
CID2 `0x42`, `0x44`, `0x61`, and `0x63` read requests.

CID2 `0x42` is authoritative for each battery's cells, voltage, signed
current, and temperatures. When 15 cells are returned, cell 16 is calculated
from that same battery's CID2 `0x42` voltage. CID2 `0x61` is only a system
voltage/SOC cross-check and is never used to reconstruct a cell. Victron DVCC
remains enabled and unchanged.

## Current commissioning boundary

The flow starts in `TEST` mode. It calculates the selected-battery 2 A current
PI command, SOC hysteresis, Vmax stops, CCL-zero stops, and recovery decisions,
but its command remains TEST-only and cannot modify DVCC or chargers. ACTIVE
is still a commissioning gate: it requires fresh complete telemetry, a valid
configuration, output readback, virtual-BMS selection, verified propagation to
the MPPTs and MultiPlus, and separate explicit activation approval.

Automatic selection starts only above 98% SOC and a selected sequence ends at
97% SOC or below. A positive BMS CCL is always a physical ceiling; CCL zero
stops charging and starts natural discharge recovery. The controller never
uses direct charger D-Bus writes.

Run locally:

```text
npm test
npm run build-flow
npm run check
```

The root-supervised RS485 service owns the optional FTDI adapter and starts the
read-only Python poller as `nodered`. Node-RED reads the latest JSONL snapshot;
it does not launch a second serial client. The generated flow is
`flow/cerbo-balancer-controller.json`. Import it by merging it with the
existing Node-RED flow, retaining the existing `victron-client` configuration
node. Dashboards are available at `/dashboard/balancer` and
`/dashboard/balancer-maintenance`. The maintenance page refreshes every five
seconds and shows system SOC/voltage, limits, per-battery voltage/current,
cell voltages, per-battery and all-pack spread, temperatures, inventory
recovery status, serial ownership, reconnect count, and USB/serial errors.
Short interruptions show the last valid measurements as `STALE` for up to ten
seconds; controller safety remains invalid during that period.

Persistent runtime files are stored on Cerbo under `/data/home/nodered/`:

```text
/data/home/nodered/cerbo-balancer-config.json
/data/home/nodered/cerbo-balancer-state.json
/data/home/nodered/cerbo-balancer-rs485-inventory.json
/data/home/nodered/cerbo-balancer-events.jsonl
/data/home/nodered/cerbo-balancer-sessions.jsonl
```

Detailed RS485 telemetry is written as parsed hourly JSONL segments below
`/data/home/nodered/cerbo-balancer-telemetry/` and retained for 24 hours. The
latest parsed snapshot is `/data/home/nodered/cerbo-balancer-latest.json`.
The compact monthly summary is written every 60 seconds to
`/data/home/nodered/cerbo-balancer-summary.jsonl` and retained for 30 days.
Raw RS485 frames and protocol payloads are not persisted.

Install `deploy/cerbo-balancer-rs485-run` as a root-supervised runit service.
It exclusively claims `ttyUSB0`, stops Venus generic serial services assigned
to that port, and repeats the handoff after a USB reconnect. CAN, DVCC, and
unrelated serial ports are not changed.

No credentials are stored in this repository.

See [RS485 investigation](docs/rs485-investigation.md), [dashboard operation](docs/dashboard.md), and [DVCC handover](docs/dvcc-handover.md).
