# Direct-CAN-first commissioning

1. Confirm `can1` is already configured at 500 kbit/s and is error-active.
2. Start the receive-only reader on `can1`. It opens a SocketCAN raw receive
   socket and emits timestamped JSON; it has no transmit operation.
3. Retain raw captures as fixtures and compare their timestamps and values to
   read-only Cerbo D-Bus values for pack voltage, current, SOC, CCL, DCL, CVL,
   and alarms.
4. Do not assign meaning to `0x351`, `0x355`, `0x356`, `0x359`, `0x3FE`, or
   `0x3FF` from plausibility alone. A cell mapping must be repeatable and
   independently validated.
5. Until Vmax, Vmin, and indexes are validated, the adapter reports cell
   telemetry unavailable and ACTIVE remains blocked.

The flow logs raw CAN telemetry and controller sessions under
`/data/home/nodered/`. No interface configuration, wake frame, BMS control
frame, or CAN transmission is used.

## Rollback

Before deployment, save the complete Node-RED flow JSON. Restore that backup
through the Node-RED editor/API if the balancer flow must be removed. The
balancer has no charger-output node, so removing it cannot change DVCC or a
charge-voltage setting.
