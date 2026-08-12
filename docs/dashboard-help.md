## Dyness Balancer

`TEST` is shadow mode. `ACTIVE` is available only after the RS485 virtual BMS
is selected manually in Cerbo and output readback is verified.

The controller uses three states: `NORMAL`, `BALANCING`, and `SAFETY_STOP`.
Automatic balancing defaults to ON after a fresh state or Restore Defaults.
When it is ON, the first eligible battery is selected automatically; there is
no separate Start command. Turning it OFF releases the selection, resets the
current controller, and keeps the state in `NORMAL` using the Cerbo Charge
Control voltage and current. It does not disable charging.

The Cerbo battery-monitor setting remains the only selector between the RS485
virtual BMS and normal Dyness CAN BMS. `TEST`/`ACTIVE` controls whether the
calculated request can be applied through the selected virtual BMS; it does not
change the selected BMS source.

It selects the first battery in address order whose addressed CID2 `0x61`
spread is strictly above 30 mV and controls aggregate charge allowance so that
battery receives approximately 2 A. CID2 `0x42` cells remain visible but do
not determine controller SOC, Vmin, Vmax, or spread.

Cloud-limited current holds the selected battery and freezes feed-forward and
PI after four consecutive deficient eight-second samples. Master CID2 `0x63`
limits and permission remain authoritative. A selected battery's local MOSFET
or protection interruption only excludes that battery; it never switches off
charging for the remaining parallel batteries.

All expected integer SOC values at 100 complete and latch a session. The latch
rearms only after master charge permission is observed OFF and then ON for five
seconds. Effective discharge, SOC below 100, and spread strictly below 30 mV
also complete the selected session without latching.

`SAFETY_STOP` requests the conservative 55.0 V / 10.0 A charge-capable fallback.
Valid master permission, CCL, CVL, and thermal limits can reduce that output.
No software cell-voltage stop or forced charge/discharge cycle is used.
