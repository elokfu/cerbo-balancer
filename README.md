# Cerbo Dyness Balancer

Dyness/Pylon-compatible RS485 telemetry and a TEST-mode active-balancing
controller for Cerbo GX. The RS485 adapter is polled at 115200 8N1 using only
CID2 `0x42`, `0x44`, `0x61`, and `0x63` read requests.

CID2 `0x42` is authoritative for each battery's cells, voltage, signed
current, and temperatures. When 15 cells are returned, cell 16 is calculated
from that same battery's CID2 `0x42` voltage. Addressed CID2 `0x61` supplies
each battery's integer SOC, Vmin, Vmax, spread, and packed locations. These
values alone drive selection and completion; the CID2 `0x42` cell array is
diagnostic-only for those decisions.

## Current commissioning boundary

The flow starts in `TEST` mode. It calculates a feed-forward plus slow-PI
aggregate-current request that targets 2 A in the first battery whose addressed
CID2 `0x61` spread is strictly above 30 mV. ACTIVE
is still a commissioning gate: it requires fresh complete telemetry, a valid
configuration, output readback, virtual-BMS selection, verified propagation to
the MPPTs and MultiPlus, and separate explicit activation approval.

The selected battery remains locked until all expected batteries report integer
SOC 100, a qualifying effective-discharge completion occurs, or its local
charge path/protection excludes it. Cloud-limited charging freezes control
without changing state. There are no forced discharge cycles or software cell
voltage stops. Master CID2 `0x63` permission and limits remain authoritative.

In `NORMAL`, requested charge voltage and current come from the Cerbo's
read-only **Settings → System Setup → Charge Control** values. The virtual BMS
also treats enabled UI limits as ceilings during balancing, in addition to the
Dyness BMS and thermal limits. Changing the GX settings therefore takes effect
on the next telemetry cycle without editing balancer configuration.

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

The service publishes the RS485 virtual BMS as DeviceInstance `100` alongside
the normal Dyness CAN BMS at instance `512`. Cerbo selection is manual at
`Settings → System Setup → Batteries → Battery monitor`; the service never
changes it. Standard D-Bus limit paths show final effective virtual-BMS
outputs, while the balancer page and logs show requested-versus-effective
arbitration details. The current CAN selection remains the default until the
commissioning handover is deliberately performed.

See [RS485 investigation](docs/rs485-investigation.md), [dashboard operation](docs/dashboard.md), and [DVCC handover](docs/dvcc-handover.md).

## Engineering algorithm reference PDF

The generated operator and engineering reference is
[`output/pdf/dyness-balancer-algorithm-reference.pdf`](output/pdf/dyness-balancer-algorithm-reference.pdf).
It documents the deployed telemetry sources, three-state controller,
virtual-BMS/DVCC arbitration, safety fallback, and CSV recording schema.

Regenerate it with ReportLab:

```powershell
python docs/pdf/generate_dyness_balancer_pdf.py
```

The generator uses vector ReportLab diagrams and writes the stable output name
under `output/pdf/`.
