# Dyness B3 RS485 investigation

Investigation date: 2026-08-07
Target: Cerbo GX `192.168.178.43`
Battery: Cerbo identifies the active service as `DYNESS-L BATTERY`; the
installation is being treated as Dyness B3.

## Result

The USB adapter is detected as an FTDI FT232R:

```text
USB VID:PID 0403:6001
Serial: A602K5MM
Device: /dev/ttyUSB0
Stable path: /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A602K5MM-if00-port0
```

The Cerbo’s serial-starter automatically creates several one-shot hardware
detectors for `/dev/ttyUSB0`, including generic Modbus, FZ Sonick, and IMT
RS485 services. Their logs show generic baud scans and no responses. These
scans are not a validated Dyness exchange and are not used as decoder input.
No additional balancer serial client was started.

The existing CAN path remains healthy:

```text
interface: can1
bitrate: 500000
state: ERROR-ACTIVE
restart-ms: 0
```

The read-only Victron setting check returned the existing maximum charge
voltage of `54.7 V`. No setting write, DVCC change, CAN configuration change,
battery restart, or DIP-switch change was performed by this investigation.

## Physical/protocol boundary

The official Dyness B3 manual documents the CAN/485 connector as:

```text
RJ45 pin 1: 485A
RJ45 pin 2: XGND
RJ45 pin 3: 485B
```

It documents 9600 baud as the default and 115200 as the alternate setting.
The B3 communication mode is host/inverter-specific; the manual does not
provide a generic register map that can safely be used for arbitrary polling.
All four DIP positions were reported as OFF (`0000`). In a multi-battery
installation that is normally the slave setting. A single B3 acts as its own
host, but the correct host protocol still depends on the connected client.

## User-provided candidate register map

The following map was supplied for investigation. It is retained as an
**unverified candidate** and is not used by the deployed flow:

| Address | Candidate meaning | Encoding |
| --- | --- | --- |
| 0 | Pack current | signed 16-bit, `/100 A` |
| 1 | Pack voltage | unsigned 16-bit, `/100 V` |
| 2 | SOC | low byte, `%` |
| 3 | SOH | low byte, `%` |
| 21–36 | Cell voltages 1–16 | unsigned 16-bit, `/1000 V` |
| 49–52 | Temperature sensors 1–4 | signed 16-bit, `/10 °C` |

The supplied map does not specify the required Modbus function code, parity,
CRC convention, address base, or whether it applies to Dyness B3. It also
resembles generic/Pace-style battery maps rather than a register table in the
official B3 manual. The repository now contains a narrow read-only probe that
defaults to 9600 8N1 and also permits an explicitly selected 115200 8N1 retry.
It tries functions 03 and 04 and the first candidate block at address 1 first.
If there is no valid response, it scans legal Modbus addresses 2–247 and stops
at the first validated response, then reads the remaining candidate blocks from
that address. It must only be run after the Cerbo serial autodetection services
are stopped and with a documented rollback.

Reference: [Dyness B3 User Manual](https://dyness.com/Public/Uploads/uploadfile/files/20241023/B3UserManualEN.pdf).

## Decision

RS485 telemetry is currently **unavailable**. The balancer must not interpret
generic Modbus responses, plausible numbers, or unmatched bytes as battery or
cell data. No RS485 request implementation is included until Dyness provides
the exact B3 protocol or a documented compatible inverter protocol.

The direct-CAN reader remains receive-only. DVCC, `can1`, battery DIP switches,
and charger settings remain unchanged.

## Live probe result after polarity reversal

The temporary Venus serial detectors were stopped and the adapter was tested
exclusively at both documented rates. For each rate, address 1 was tested
first; because it returned no bytes, addresses 2–247 were then tested. Both
Modbus functions 03 and 04 were used for the candidate aggregate block at
registers 0–3.

```text
9600 8N1: 0 valid responses from addresses 1–247
115200 8N1: 0 valid responses from addresses 1–247
```

No response bytes, exception frames, or CRC-valid frames were received after
the A/B polarity reversal. The remaining candidate blocks were therefore not
requested. The temporary serial probe was removed, the Cerbo serial-starter
link was recreated, and the Cerbo resumed ownership of `/dev/ttyUSB0`.

## Live probe after user DIP change

After the user changed the B3 DIP setting, the read-only probe was run again at
9600 8N1. Address 1 was tested first, followed by legal addresses 2–247, using
Modbus functions 03 and 04 against registers 0–3. The run completed with 494
requests and no response bytes:

```text
9600 8N1 after DIP change: 0 valid responses from addresses 1–247
494 short responses; no exception frames or CRC-valid frames
```

The serial-starter link is intentionally left disabled until the next Cerbo
reset, at the user's request. No battery write, wake, configuration, CAN,
DVCC, or charger command was sent.

## Next permitted test

Only after a documented protocol is available may a controlled client send one
read-only request at a time with confirmed baud, parity, address, function,
register, and CRC. The exchange must be captured and repeatable. No write,
wake, configuration, or control frame is permitted.
