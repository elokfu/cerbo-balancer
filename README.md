# Cerbo Dyness Balancer

Receive-only direct-CAN telemetry validation and a TEST-mode shadow
controller for the Dyness BMS on Cerbo GX `can1`.

The implementation deliberately does not decode candidate Dyness frame IDs
until a mapping is independently validated. It does not contain a CAN
transmit path, charge-voltage output node, `cansend`, or `cangen`. Victron
DVCC remains enabled and unchanged.

## Current commissioning boundary

The deployed flow runs in `TEST` mode. It captures raw frames and combines
them with read-only D-Bus measurements, but emits only simulated voltage
intents to diagnostics. It never writes a charger setting. ACTIVE is rejected
until cell telemetry, configuration, PI gains, and output readback are all
valid. Since the current Cerbo D-Bus snapshot has no validated Vmax/Vmin cell
source, ACTIVE is expected to remain locked out.

Run locally:

```text
npm test
npm run build-flow
npm run check
```

The generated flow is `flow/cerbo-balancer-controller.json`. Import it by
merging it with the existing Node-RED flow, retaining the existing
`victron-client` configuration node. The dashboard is available at
`/dashboard/balancer` after deployment.

Persistent runtime files are stored on Cerbo under `/data/home/nodered/`:

```text
cerbo-balancer-config.json
cerbo-balancer-state.json
cerbo-balancer-events.jsonl
cerbo-balancer-telemetry.jsonl
cerbo-balancer-sessions.jsonl
```

No credentials are stored in this repository.

See [direct CAN testing](docs/direct-can-test.md), [RS485 investigation](docs/rs485-investigation.md), [dashboard operation](docs/dashboard.md), and [DVCC handover](docs/dvcc-handover.md).
