'use strict'

const assert = require('assert/strict')
const { UNAVAILABLE_REASON, unavailableStatus } = require('../src/rs485_status')

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
