'use strict'

const UNAVAILABLE_REASON = 'no validated Dyness B3 RS485 protocol or response'

function unavailableStatus (overrides = {}) {
  return {
    available: false,
    valid: false,
    source: 'rs485-unavailable',
    interface: '/dev/ttyUSB0',
    baudRate: null,
    parity: null,
    slaveAddress: null,
    reason: UNAVAILABLE_REASON,
    ...overrides,
    available: false,
    valid: false
  }
}

module.exports = { UNAVAILABLE_REASON, unavailableStatus }
