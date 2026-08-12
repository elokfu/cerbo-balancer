# DVCC handover gate

Direct-CAN read-only testing and shadow calculations do not disable DVCC.
Before any future handover, back up settings and validate every active
charging path: MultiPlus/VE.Bus, MPPT RS charger 0, MPPT RS charger 1, and
other chargers. Confirm how each receives charge-voltage and charge-current
limits and verify output readback.

The effective charge ceiling is:

```text
min(controllerSafetyCeiling, BMS_CVL, configuredDeviceCeiling)
```

Never exceed a valid BMS CCL. Preserve CVL, CCL, DCL, charge/discharge
permissions, and alarms. `CCL = 0 A` remains an advertised limit and must be
logged without falsely declaring the battery disconnected. If DCL or
permissions cannot be preserved, DVCC handover is prohibited.

Disabling DVCC is a separate manually confirmed commissioning step with a
settings backup and tested rollback. It is not part of this repository's
initial deployment.

## Manual virtual-BMS selection

The RS485 virtual BMS is published as
`com.victronenergy.battery.rs485_dyness`, DeviceInstance `100`, and is
available to Cerbo as `com.victronenergy.battery/100`. The existing Dyness CAN
BMS remains `com.victronenergy.battery/512`.

Select the source manually at **Settings → System Setup → Batteries → Battery
monitor**. Selecting the virtual source makes its standard effective CVL,
CCL, DCL, and permission paths the DVCC input. Selecting CAN restores the
normal BMS. Authority becomes `APPLIED`, `SHADOW`, or `UNKNOWN` directly from
this readback; there is no separate TEST/ACTIVE switch. The service never
writes this setting.

The Automatic balancing switch does not select a BMS. OFF keeps the controller
in NORMAL and requests the Cerbo Charge Control limits through whichever BMS
the operator selected. Selecting the normal CAN BMS is the explicit way to
bypass RS485 virtual-BMS authority.

The virtual service publishes both layers: standard paths show final effective
values, while `/Control/*`, the balancer page, CSV output, and parsed snapshots
show controller-requested values, freshness, thermal derating, active source,
and arbitration reason. VRM is expected to show the standard effective battery
values; requested-versus-effective diagnostics remain on the local balancer
page because arbitrary custom D-Bus paths are not guaranteed to be graphed by
VRM.
