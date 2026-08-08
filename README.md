# Cerbo Dyness Balancer

Read-only Dyness/Pylon-compatible RS485 telemetry and a TEST-mode shadow
controller for Cerbo GX. The RS485 adapter is polled at 115200 8N1 using only
CID2 `0x42`, `0x61`, and `0x63` read requests.

CID2 `0x42` is authoritative for each battery's cells, voltage, signed
current, and temperatures. When 15 cells are returned, cell 16 is calculated
from that same battery's CID2 `0x42` voltage. CID2 `0x61` is only a system
voltage/SOC cross-check and is never used to reconstruct a cell. Victron DVCC
remains enabled and unchanged.

## Current commissioning boundary

The flow runs in `TEST` mode. It emits simulated voltage/current intents to
diagnostics and never writes a charger setting. ACTIVE is rejected until
fresh, validated per-battery 16-cell telemetry, configuration, PI gains, and
output readback are all valid. A disconnected adapter produces an explicit
unavailable snapshot and conservative virtual-BMS values.

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
/data/home/nodered/cerbo-balancer-telemetry.jsonl
/data/home/nodered/cerbo-balancer-sessions.jsonl
```

Install `deploy/cerbo-balancer-rs485-run` as a root-supervised runit service.
It exclusively claims `ttyUSB0`, stops Venus generic serial services assigned
to that port, and repeats the handoff after a USB reconnect. CAN, DVCC, and
unrelated serial ports are not changed.

No credentials are stored in this repository.

See [RS485 investigation](docs/rs485-investigation.md), [dashboard operation](docs/dashboard.md), and [DVCC handover](docs/dvcc-handover.md).
