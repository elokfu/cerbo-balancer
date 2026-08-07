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
official B3 manual. Consequently, polling all 255 addresses with guessed
framing would not be a validated test and is intentionally not performed while
Venus serial autodetection owns `/dev/ttyUSB0`.

Reference: [Dyness B3 User Manual](https://dyness.com/Public/Uploads/uploadfile/files/20241023/B3UserManualEN.pdf).

## Decision

RS485 telemetry is currently **unavailable**. The balancer must not interpret
generic Modbus responses, plausible numbers, or unmatched bytes as battery or
cell data. No RS485 request implementation is included until Dyness provides
the exact B3 protocol or a documented compatible inverter protocol.

The direct-CAN reader remains receive-only. DVCC, `can1`, battery DIP switches,
and charger settings remain unchanged.

## Next permitted test

Only after a documented protocol is available may a controlled client send one
read-only request at a time with confirmed baud, parity, address, function,
register, and CRC. The exchange must be captured and repeatable. No write,
wake, configuration, or control frame is permitted.
