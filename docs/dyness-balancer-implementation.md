# Dyness PowerBrick PRO Balancer and Power-Safe BMS Guardian

Engineering implementation, algorithm, commissioning, and recovery reference

Implementation date: 2026-08-19

This document describes the deployed Cerbo GX solution in the
`codex/rs485-dyness-balancer` branch. It distinguishes measured Dyness
telemetry from project-specific control policy. It is not a Dyness
manufacturer specification and does not replace battery, inverter, or
electrical safety documentation.

## 1. Purpose and system boundary

The installation has three parallel Dyness PowerBrick PRO batteries, addressed
2, 3, and 4. The solution provides:

- Read-only Dyness/Pylon-compatible RS485 telemetry.
- Per-battery cells, current, voltage, status, capacity SOC, and diagnostics.
- Reconstruction of the physical sixteenth cell when only cells 1-15 are sent.
- Automatic selection and current control for passive balancing.
- A persistent Victron D-Bus guardian that remains selected during worker
  restarts and RS485 communication failures.
- A fixed communication fallback of 54.0 V CVL, 20.0 A CCL, and 100.0 A DCL.
- Node-RED management pages, event history, persistence, and CSV logging.

The RS485 link is read-only. No Dyness write, wake, configuration, MOSFET,
charge, or discharge command is transmitted. Dyness physical protection is
the final hardware safety layer.

### 1.1 Component architecture

| Component | Runtime identity | Responsibility |
| --- | --- | --- |
| Dyness batteries | RS485 addresses 2, 3, 4 | Physical cells, BMS protection, telemetry, and limits |
| RS485 worker | `cerbo-balancer-rs485` | Polling, validation, estimation, snapshot, logging |
| Node-RED controller | `node-red-venus` | Balancing state machine and requested CVL/CCL |
| BMS guardian | `cerbo-balancer-guardian` | Persistent D-Bus publication and communication fallback |
| Guardian D-Bus service | `com.victronenergy.battery.rs485_dyness_guardian` | Selected battery/BMS, DeviceInstance 101 |
| Cerbo DVCC | Victron system service | Distributes selected BMS limits to chargers and inverter |

Normal data flow is:

```text
Dyness RS485 -> telemetry worker -> atomic JSON snapshot -> Node-RED request
       -> worker arbitration data -> guardian instance 101 -> DVCC -> chargers
```

The worker runs in `telemetry-only` publisher mode. The former worker D-Bus
instance 100 is not selected and is retained only as a rollback architecture.
The guardian at DeviceInstance 101 is the permanent selected battery monitor
and controlling BMS.

## 2. Hardware and communication setup

### 2.1 PowerBrick PRO DIP switch

Set the five PowerBrick PRO communication DIP switches to:

```text
00110
```

Read the positions in the order printed on the battery. This setting selects
the **Victron and others** protocol profile at **115200 baud**. The deployed
serial format is 115200 baud, 8 data bits, no parity, 1 stop bit (`115200 8N1`).

Confirm switch numbering and ON/OFF orientation against the label on the
specific battery revision before energizing the communication link.

### 2.2 Physical serial interface

The worker uses the stable FTDI path:

```text
/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A602K5MM-if00-port0
```

The runit wrapper invokes the Venus `stop-tty.sh ttyUSB0` handoff before
opening the port. A kernel `flock` prevents duplicate workers. The process runs
as `nodered:nodered`, while the root wrapper monitors port ownership and
reapplies the handoff only if another process opens the adapter.

## 3. RS485 protocol and validation

The protocol uses printable ASCII frames beginning with `~` and ending with a
four-hex-digit checksum plus carriage return. Requests use protocol version
`0x20`, CID1 `0x46`, and only read CID2 values.

For an ASCII body with bytes `b_j`, the frame checksum is:

```text
checksum = (-sum(j=1..N, b_j)) mod 65536
```

The INFO length is a 12-bit value with a leading nibble checksum:

```text
L = 0xABC
length_check = (-(A + B + C)) mod 16
encoded_length = hex(length_check) || hex12(L)
```

Every response is rejected unless framing, ASCII encoding, checksum, address,
CID1, return code, length checksum, INFO length, and CID-specific structure are
valid.

### 3.1 CID2 responsibilities

| CID2 | Source and content | Use in this implementation |
| --- | --- | --- |
| `0x42` | Addressed battery cells, temperatures, signed current, pack voltage, optional capacity tail | Per-battery telemetry, Cell 16 estimator, capacity SOC |
| `0x44` | Status1-Status5, MOSFET states, protection, alarms, cell-check bits | Diagnostics and local eligibility; never an SOC source |
| `0x61` | Master system voltage, current, integer SOC, global cell extrema and packed IDs, health and temperatures | System SOC, global extrema bounds, guardian and DVCC authority |
| `0x63` | CVL, DVL, CCL, DCL, permission/state byte | Final BMS limits and charge/discharge permissions |

Only master address 2 is polled for `0x61` and `0x63`. Expected no-reply from
slave addresses is not a communication fault. The capacity-derived per-battery
SOC comes from the extended tail of `0x42`; `0x44` is status-only.

### 3.2 Units and signed values

```text
cell voltage       = raw_u16 / 1000 V
pack voltage       = raw_u16 / 1000 V
battery current    = raw_s16 / 100 A
temperature        = raw_u16 / 10 - 273.15 deg C
capacity           = raw_u24 / 1000 Ah
capacity SOC       = 100 * remaining_mAh / total_mAh
```

Physical validation rejects cells outside 2.0-4.5 V, temperatures outside
-40 to 100 deg C, impossible capacity values, malformed structural markers,
and incomplete active-battery snapshots.

### 3.3 Polling and inventory

- Primary telemetry cadence: approximately 8 seconds.
- CID2 `0x44` diagnostic cadence: 5 seconds with a bounded timeout.
- Normal discovery: addresses 2-16, completed every 60 seconds.
- Recovery discovery: complete scan every 10 seconds.
- Confirmed removal: 10 consecutive failed complete scans.
- Serial reconnect backoff: 2 seconds.
- Controller and guardian source freshness: 20 seconds.

Normal discovery performs only two short probes per primary cycle. This keeps
discovery from monopolizing the serial bus. A partial current sum is never
published as authoritative.

## 4. Cell 16 reconstruction

Cells 1-15 are reported directly at approximately 1 mV resolution. Cell 16 is
physical but may not be sent directly. The same battery's pack voltage has
approximately 10 mV resolution, so direct subtraction is too noisy for
balancing control.

For battery `b`, let `V_b,i` be directly reported cells 1-15 and `V_b,pack` be
that battery's own CID2 `0x42` pack voltage.

### 4.1 Robust common-cell voltage

Sort the 15 direct cells, discard the lowest three and highest three, then
average the central nine:

```text
ordered = sort(V_b,1 ... V_b,15)
V_b,common = (1/9) * sum(i=4..12, ordered_i)
```

This follows common pack movement while rejecting balancing outliers.

### 4.2 Raw observation and relative offset

```text
V_b,16,raw = V_b,pack - sum(i=1..15, V_b,i)
o_b,raw    = V_b,16,raw - V_b,common
```

The raw subtraction is retained for diagnostics only. It never directly drives
Vmin, Vmax, spread, imbalance detection, or balancing.

Each battery has independent estimator state. No offset, baseline, rolling
window, or previous result is shared across addresses.

### 4.3 Quantization-aware filtered offset

The first valid raw offset becomes the subtraction baseline. Later changes are
estimated relative to that baseline. Maintain the five latest raw offsets and
use their median as the target:

```text
o_b,target = median(last_5(o_b,raw))
e_b        = o_b,target - o_b,filtered
```

The combined uncertainty of pack quantization and summing fifteen 1 mV cells
is treated as an interval. Errors within approximately 12.5 mV do not force an
instantaneous correction. Outside the interval, correction is limited and
filtered:

```text
e_b,limited = clamp(e_b, -0.020, +0.020) V
o_b,filtered[k] = o_b,filtered[k-1] + g * e_b,limited
```

Normal gain is `g = 0.15`. If the qualifying error remains above 10 mV with
the same sign for five consecutive valid samples, gain becomes `g = 0.30`.
Acceleration clears immediately when magnitude or sign no longer qualifies.

The unconstrained estimate is:

```text
V_b,16,candidate = V_b,common + o_b,filtered
```

### 4.4 CID2 0x61 global bounds

Fresh global extrema provide physical bounds without imposing the old
instantaneous `Vpack +/- 5 mV` clamp:

```text
V61,min <= V_b,16,published <= V61,max
```

If a packed `0x61` extremum ID identifies this battery and Cell 16, the exact
reported extremum is published for that sample. Otherwise the candidate is
only range-clamped. Both battery address and cell number must match; another
cell at the global minimum must not force Cell 16 to that minimum.

The published Cell 16 participates normally in per-battery extrema, spread,
imbalance detection, balancing selection, dashboard telemetry, and CSV data.
Diagnostics retain raw, common, target, filtered offset, gain, residual,
constraint source, constraint application, and corroboration state.

Estimator state resets after 20 seconds stale, invalid or incomplete cell/pack
data, confirmed removal/address replacement, or a pack-voltage jump above
0.5 V. A duplicate sample returns the prior result and does not advance state.

## 5. Per-battery capacity SOC and coulomb interpolation

The validated `0x42` extended tail contains 24-bit remaining and total mAh.
Legacy-only capacity tails are rejected.

### 5.1 SOC used by balancing

For battery `b`:

```text
SOC_b,raw = 100 * R_b,mAh / T_b,mAh
SOC_b,int = floor(SOC_b,raw)
```

`SOC_b,int` is the only independent per-battery SOC used for balancing
selection and completion. The interpolated value is display and CSV telemetry
only. Global charge derating, guardian `/Soc`, DVCC, and dynamic-float reset
continue to use master CID2 `0x61` SOC.

### 5.2 Coulomb integration between capacity steps

For unique samples `k-1` and `k`, trapezoidal charge is:

```text
Delta_t_h = (t_k - t_(k-1)) / 3600000
I_avg     = (I_(k-1) + I_k) / 2
Delta_Ah  = I_avg * Delta_t_h
```

The display estimator advances as:

```text
SOC_hat[k] = SOC_hat[k-1]
             + 100 * g_direction * Delta_Ah / T_b,Ah
```

Initial gains are `g_charge = 1.04` and `g_discharge = 1.08`. Positive current
uses charge gain; negative current uses discharge gain.

Whenever `remainingCapacityRawMah` changes below 100%, the estimator:

1. Evaluates the completed interval for gain learning.
2. Re-anchors `SOC_hat` to `SOC_b,raw`.
3. Clears accumulated interval Ah.
4. Resumes integration until the next raw capacity transition.

With the observed 280 Ah packs and 2.8 Ah capacity steps, raw anchors coincide
with integer percentage transitions. The code triggers on changed remaining
mAh and anchors to raw percentage, not explicitly to the floored integer.

### 5.3 Directional gain learning

For a valid interval:

```text
Delta_Ah_observed = (R_k,mAh - R_anchor,mAh) / 1000
g_observed        = Delta_Ah_observed / Delta_Ah_integrated
g_new             = 0.80 * g_old + 0.20 * g_observed
```

Training requires matching signs, samples no more than 20 seconds apart,
below-100% endpoints, and `0.90 <= g_observed <= 1.20`. An implausible interval
re-anchors without training and emits `SOC_CAPACITY_CORRECTION`.

### 5.4 Voltage-based full anchor

Capacity-reported 100% alone does not anchor interpolation. For each battery
independently:

```text
if max(effective_cells_b) >= 3.500 V:
    SOC_hat = 100.000%
    interval_Ah = 0
```

The effective 16-cell vector includes reconstructed Cell 16. While the
condition remains true, positive charging current is not integrated. When Vmax
falls below 3.500 V, discharge integration can reduce SOC immediately.

Invalid capacity data or a gap above 20 seconds discards unfinished
integration but retains learned gains. State and gains are written atomically
with a validated backup.

## 6. Balancing controller

The Node-RED controller has `NORMAL`, `BALANCING`, and `SAFETY_STOP` states.
Automatic balancing defaults ON after a fresh state or Restore Defaults.

### 6.1 Entry qualification

For each battery independently, entry requires eight consecutive unique valid
telemetry samples satisfying all of:

- Per-battery spread strictly greater than 30 mV.
- Charge MOSFET ON.
- No local hard protection.
- Valid complete telemetry and valid independent integer capacity SOC.
- Completion latch not blocking automatic selection.

Exactly 30 mV does not qualify. Failure of any requirement resets only that
battery's entry counter. Among qualified batteries, deterministic ascending
address order is used.

### 6.2 Selection and immediate exits

The selected address remains locked during balancing. Full-SOC and protection
exits are immediate. If a local charge path is interrupted, only that battery
is released; normal charging for the other parallel batteries is preserved.

All expected batteries at `SOC_b,int = 100%` complete and latch the full-SOC
session. The latch rearms only after master charge permission is observed OFF
and then continuously ON for 5 seconds.

### 6.3 Qualified low-spread exit

The selected battery exits balancing after eight consecutive unique samples:

```text
I_selected > 1.5 A
spread_selected < 0.030 V
```

Exactly 1.5 A or 30 mV does not qualify. Any failed sample resets the exit
counter.

### 6.4 Non-balancing SOC charge-current caps

In `NORMAL`, master CID2 `0x61` SOC selects an editable per-battery CCL
ceiling. The controller multiplies it by the confirmed battery inventory:

\[
I_{\mathrm{CCL,SOC}} = I_{\mathrm{per\ battery}}(SOC)\,N_{\mathrm{confirmed}}
\]

| SOC | Default per battery | Three-battery aggregate |
| --- | ---: | ---: |
| below 97% | none | none |
| 97% | 15 A | 45 A |
| 98% | 6 A | 18 A |
| 99% and above | 3 A | 9 A |

`N_confirmed` is the count of unique configured `expectedAddresses`, not the
number of batteries responding to the latest poll. This prevents a temporary
missing response from lowering the multiplier. Each value is editable on the
controller page and validated as greater than zero and at most 100 A/battery.

These caps do not apply in `BALANCING`, where selected-battery current control
is authoritative subject to final BMS, UI, thermal, and permission ceilings.
## 7. Feed-forward plus slow PI current control

The controller regulates selected-battery current by changing aggregate CCL.
It does not assume equal sharing among parallel batteries.

Let `I_s` be selected-battery current, `I_pos` the sum of positive battery
currents, and `I_target = 2.0 A`.

When `I_s >= 0.25 A` and `I_pos >= 0.5 A`:

```text
share_raw[k] = clamp(I_s / I_pos, 0.001, 1.0)
share_f[k]   = alpha * share_raw[k] + (1-alpha) * share_f[k-1]
alpha        = 0.20
```

Feed-forward and control error are:

```text
I_ff           = I_target / share_f
I_ff,effective = K_ff * I_ff
e              = I_target - I_s
P              = Kp * e
```

The discrete integral approximates the continuous integral:

```text
I_term(t) = clamp(Integral[t0,t](Ki * e(tau) d tau), -Imax, +Imax)

I_term[k] = clamp(I_term[k-1] + Ki * e[k] * Delta_t,
                  -Imax, +Imax)
```

Production defaults are `K_ff = 1.0`, `Kp = 0.20`, `Ki = 0.02`, and
`Imax = 10 A`. The unconstrained request is:

```text
I_request_raw = I_ff,effective + P + I_term
I_request     = clamp(I_request_raw, aggregate_min, aggregate_max)
```

Upward movement is slew-limited to 10 A/min:

```text
I_request[k] <= I_request[k-1] + (10 A/min) * Delta_t_min
```

Downward correction is immediate. Anti-windup prevents positive integral
growth while the output is saturated. Duplicate Node-RED ticks return the
held request and do not integrate twice.

### 7.1 Solar-limited pause

After four deficient telemetry samples, the controller pauses PI and
feed-forward updates without releasing selection when both selected current
and total current demonstrate insufficient available charging power. Recovery
also requires four qualifying samples. BMS-limited, thermal-limited,
permission-off, non-positive selected current, and CCL-zero states freeze
control rather than integrating against an unavailable actuator.

## 8. Centered dynamic-float estimator

The dynamic float uses master CID2 `0x61` system pack voltage and global Vmax.
The cell trigger is editable on the controller page as
`floatCellVoltageThreshold`.

```text
default trigger = 3.450 V/cell
allowed range   = 3.000 ... 3.650 V/cell
```

Changing the trigger invalidates the old learned float and clears incomplete
acquisition state.

### 8.1 Centered 4+1+4 acquisition

Maintain a four-value ring buffer while Vmax is below the trigger. At the first
accepted crossing, form a window with:

```text
[four samples before] + [crossing sample] + [four samples after]
```

The learned voltage is the arithmetic mean:

```text
V_float = (1/9) * sum(k=-4..+4, V61,pack[k])
```

The four post samples are accepted only while Vmax is at or above the trigger.
If Vmax falls below after crossing, acquisition pauses and preserves the
partial window; a later qualifying sample resumes it. A crossing without four
pre-trigger samples is ignored and must be rearmed below threshold.

Invalid, stale, incomplete, or duplicate telemetry cannot add a sample.
Invalid telemetry resets an incomplete window but retains a qualified float.
Master SOC at or below 98% resets the learned voltage to the 56.5 V default
and clears all transient acquisition state.

### 8.2 Float application

The learned float becomes a CVL candidate only when full-SOC balancing has
completed, the completion latch is set, and master CID2 `0x61` SOC is 100%.
It is not used as the balancing CVL during active balancing.

Final normal CVL is:

```text
CVL_normal = min(56.5 V hard ceiling,
                 valid Dyness CVL,
                 enabled Cerbo UI CVL,
                 learned/default float when active)
```

## 9. Limit arbitration and permissions

The controller proposes values; the runtime independently applies lower
authoritative ceilings.

In normal operation:

```text
CCL_candidate = min(controller request,
                    valid Dyness CCL,
                    enabled Cerbo UI CCL,
                    applicable normal SOC cap)

CCL_effective = CCL_candidate * thermal_factor
```

In balancing, the normal SOC cap is omitted. Valid Dyness permission OFF or
CCL zero forces charge allowance to zero. DCL and discharge permission remain
independent of charge control. The service never converts missing limits into
invented high limits.

Standard D-Bus outputs include:

- `/Info/MaxChargeVoltage`
- `/Info/MaxChargeCurrent`
- `/Info/MaxDischargeCurrent`
- `/Bms/AllowToCharge`
- `/Bms/AllowToDischarge`
- `/Dc/0/Voltage`, `/Dc/0/Current`, `/Dc/0/Power`
- `/Soc`

Diagnostic `/Control/*` paths expose requested values, effective values,
freshness, thermal factor, authority, and reason.

## 10. Power-safe BMS guardian

The guardian is an independent locked runit process. It reads the worker's
atomic latest snapshot once per second and remains the selected D-Bus battery
through worker updates, failures, and restarts.

### 10.1 Source validity

A source sample is valid when:

```text
snapshot.valid == true
source age <= 20000 ms
CID2 0x61 SOC is an integer in 0..100
effectiveControl.outputValid == true
```

### 10.2 Guardian state machine

| Mode | Condition | Published behavior |
| --- | --- | --- |
| `BOOTSTRAP` | No persisted or live last-good snapshot | Ready false; do not synthesize SOC |
| `NORMAL` | Fresh valid source and recovery complete | Publish worker effective values |
| `FALLBACK` | Missing, malformed, invalid, or stale source | Publish fixed 54 V / 20 A / 100 A fallback |
| `RECOVERY` | First valid unique sample after fallback | Keep fallback until second valid unique sample |

Fallback outputs are:

```text
CVL = 54.0 V
CCL = 20.0 A
DCL = 100.0 A
AllowToCharge = true
AllowToDischarge = true
Connected = true
State = RS485_FALLBACK_54V_20A
```

Last valid CID2 `0x61` SOC is retained indefinitely and never replaced by a
synthetic startup zero. Live VE.Bus/MultiPlus voltage and current are used when
available; otherwise last-good measurements are retained. A communication
alarm and guardian diagnostics identify fallback mode and source age.

Return to `NORMAL` requires two consecutive complete, fresh, unique worker
samples. The last-good snapshot, SOC, and timestamp are persisted atomically
with a validated backup.

## 11. Persistence, dashboards, and CSV

Important runtime paths are:

```text
/data/home/nodered/cerbo-balancer-latest.json
/data/home/nodered/cerbo-balancer-state.json
/data/home/nodered/cerbo-balancer-capacity-soc.json
/data/home/nodered/cerbo-balancer-guardian-state.json
/data/home/nodered/cerbo-balancer-rs485-inventory.json
/data/home/nodered/cerbo-balancer-events.jsonl
/data/home/nodered/cerbo-balancer-csv/
/data/home/nodered/cerbo-balancer-telemetry/
```

Detailed parsed telemetry is retained for 24 hours. Compact summary JSONL is
written every 60 seconds and retained for 30 days. Raw RS485 frames are not
persisted.

The maintenance page displays system telemetry, global extrema, every
effective cell, Cell 16 estimator diagnostics, per-battery integer and
interpolated SOC, capacity anchors, learned gains, status bits, temperatures,
inventory, serial ownership, and communication health.

CSV files use a fixed schema and fixed battery inventory for each recording.
A new schema starts a new file; columns are not appended to an active legacy
schema. The current schema includes per-battery raw/integer/interpolated SOC,
anchor state, mAh/Ah, gains, Vmax/full-reset state, cell values, Cell 16
diagnostics, status bytes, controller terms, float state, authority, and final
effective limits.

## 12. Power-safe deployment and rollback

### 12.1 Worker-only deployment

1. Require guardian DeviceInstance 101 to be selected, Ready, and NORMAL.
2. Back up the worker and current latest snapshot.
3. Copy the new worker to a temporary file and validate Python syntax.
4. Preserve `nodered:nodered` ownership and atomically rename it into place.
5. Restart only `cerbo-balancer-rs485` with `svc -t`.
6. Confirm guardian immediately enters FALLBACK with exactly 54/20/100.
7. Confirm a new worker PID and two valid samples before NORMAL resumes.
8. Roll back only the worker if recovery fails; leave guardian running.

### 12.2 Node-RED deployment

1. Back up live `flows.json` and `flows_cred.json`.
2. Merge generated controller/dashboard nodes by node ID into the complete live
   flow; never replace the 81-node live flow with a partial export.
3. Validate staged JSON, preserve ownership, and atomically replace flows.
4. Restart only `node-red-venus`.
5. Allow for the Cerbo's palette-loading startup delay.
6. Confirm dashboard HTTP 200 and unchanged worker/guardian PIDs.
7. Restore the flow backup independently if Node-RED fails.

### 12.3 Guardian upgrades

Do not restart the active guardian for ordinary worker or Node-RED deployments.
Upgrade guardian code only through a validated shadow/A-B instance while the
existing selected guardian remains operational. Perform selection migration
with external AC or bypass when physical continuity is required.

## 13. Reboot behavior

`/data/rc.local` recreates runit service directories under volatile storage.
It registers the guardian before the worker, creates bounded multilog services,
and then links both supervisors into `/service`. Each process has an
independent lock and log rotation (`25 kB`, four files).

After reboot:

1. Guardian restores last-good state and selected instance 101.
2. Guardian publishes fallback until worker telemetry is valid.
3. Worker claims the FTDI adapter and starts telemetry-only polling.
4. Two unique valid snapshots return the guardian to NORMAL.
5. Node-RED restores controller configuration and state independently.

## 14. Parameter reference

| Parameter | Default | Meaning |
| --- | ---: | --- |
| RS485 serial | 115200 8N1 | PowerBrick PRO Victron/others profile |
| Poll interval | 8 s | Primary complete telemetry cadence |
| Freshness | 20 s | Controller and guardian validity bound |
| Balance spread | >30 mV | Strict entry threshold |
| Qualification | 8 samples | Entry and selected low-spread exit |
| Selected target | 2.0 A | Balancing current target |
| Exit current | >1.5 A | Strict selected-battery exit criterion |
| Feed-forward alpha | 0.20 | Current-share EWMA |
| Kp / Ki | 0.20 / 0.02 | Slow PI defaults |
| Upward slew | 10 A/min | Aggregate request rise limit |
| SOC CCL at 97% | 15 A/battery | Editable; multiplied by confirmed inventory |
| SOC CCL at 98% | 6 A/battery | Editable; multiplied by confirmed inventory |
| SOC CCL at >=99% | 3 A/battery | Editable; multiplied by confirmed inventory |
| Float trigger | 3.450 V/cell | Editable, allowed 3.000-3.650 V |
| Float window | 4+1+4 | Centered pack-voltage samples |
| Float reset SOC | <=98% | Clear learned float to 56.5 V |
| Cell 16 median | 5 samples | Raw-offset target filter |
| Cell 16 gain | 0.15 / 0.30 | Normal / persistent-error correction |
| Cell 16 correction | +/-20 mV | Per-update error clamp |
| Capacity gains | 1.04 / 1.08 | Initial charge / discharge gains |
| Gain EWMA | 0.20 | Capacity-interval learning rate |
| Guardian fallback | 54 V / 20 A / 100 A | CVL / CCL / DCL |
| Guardian recovery | 2 samples | Unique complete samples before NORMAL |

## 15. Acceptance checks

- DIP code is `00110`; serial operation is 115200 8N1.
- All active addresses return complete valid `0x42`; only master answers `0x61`.
- Cell 16 uses independent filtered state and respects correctly decoded global
  bounds without being pinned to another cell's extremum.
- Per-battery capacity SOC anchors on each remaining-mAh transition below 100%.
- Interpolated SOC never affects guardian SOC, DVCC, global CCL, or float reset.
- Balance entry and low-spread exit change only at sample eight.
- Float qualification changes only at centered sample nine.
- Changing the float trigger resets old learning.
- Worker restart leaves guardian PID and D-Bus service continuously present.
- RS485 disconnection produces persistent 54/20/100 fallback, never CCL zero.
- Recovery requires two valid unique samples.
- Reboot recreates both runit services and returns guardian to NORMAL.
- Dashboard and CSV identify raw, estimated, bounded, requested, and effective
  values without conflating their authority.

## 16. Implementation references

Primary implementation paths:

```text
scripts/dyness_rs485_protocol.py   protocol framing and CID2 decoding
scripts/dyness_rs485_service.py    worker, estimators, arbitration, logging
scripts/dyness_bms_guardian.py     persistent D-Bus guardian
src/controller.js                  balancing and dynamic-float controller
src/controller_dashboard.html      controller management interface
src/maintenance_view.html          RS485 maintenance interface
deploy/cerbo-rc.local              guardian-first reboot registration
flow/cerbo-balancer-controller.json generated Node-RED flow
```

The implementation and thresholds in this document describe the project as of
2026-08-19. Revalidate the manual whenever control logic, protocol decoding,
fallback values, D-Bus selection, or hardware configuration changes.
