# RS485 service and virtual BMS

`scripts/dyness_rs485_service.py` polls the stable FTDI path at 115200 8N1.
It discovers Dyness/Pylon addresses 2–16 and sends only read requests:

- CID2 `0x42`: per-battery cells, temperatures, current, and voltage.
- CID2 `0x61`: system voltage and SOC cross-check.
- CID2 `0x63`: charge/discharge voltage, CCL, DCL, and status.

The adapter is optional during development. If it is disconnected, the service
emits a timestamped invalid snapshot, keeps CCL/DCL at zero, and does not
allow ACTIVE controller operation.

The service owns the virtual D-Bus name
`com.victronenergy.battery.rs485_dyness` with DeviceInstance `100`. Once that
service is separately selected as the active battery monitor, GX displays:

- `/Soc` from CID2 `0x61`;
- `/Dc/0/Voltage` from CID2 `0x61`;
- `/Dc/0/Current` as the complete sum of valid CID2 `0x42` battery currents;
- `/Dc/0/Power` as voltage multiplied by summed current.

No partial current sum is published as authoritative. D-Bus publishing does
not modify DVCC, charger settings, or battery configuration.

For a Cerbo installation, copy both Python files in `scripts/` to the runtime
directory and start the service under the existing Node-RED/supervision
mechanism. The generated Node-RED flow starts the service in shadow mode and
retains the JSON snapshot for the maintenance page.
