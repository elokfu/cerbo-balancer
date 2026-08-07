'use strict'

const assert = require('assert/strict')
const { UNAVAILABLE_REASON, unavailableStatus, normalizeSnapshot } = require('../src/rs485_status')

const status = unavailableStatus({ interface: '/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A602K5MM-if00-port0' })
assert.equal(status.available, false)
assert.equal(status.valid, false)
assert.equal(status.interface.includes('FTDI_FT232R'), true)
assert.equal(status.baudRate, null)
assert.equal(status.slaveAddress, null)
assert.equal(status.reason, UNAVAILABLE_REASON)

const attemptedOverride = unavailableStatus({ available: true, valid: true })
assert.equal(attemptedOverride.available, false)
assert.equal(attemptedOverride.valid, false)

const normalized = normalizeSnapshot({
  timestamp: 1000,
  valid: true,
  cellTelemetryValid: true,
  system: { voltage61: 53.2, soc61: 91 },
  limits: { chargeVoltage: 56.5, chargeCurrent: 56, dischargeCurrentSigned: -198.8, statusRaw: 0 },
  aggregate: { summedBatteryCurrent: -4.2, vmax: 3.52, vmin: 3.49, maxCellIndex: 4, minCellIndex: 2 }
}, 1000)
assert.equal(normalized.packVoltage, 53.2)
assert.equal(normalized.packCurrent, -4.2)
assert.equal(normalized.ccl, 56)
assert.equal(normalized.valid, true)
