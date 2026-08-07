# DVCC handover gate

Direct-CAN read-only testing and the shadow controller do not disable DVCC.
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
