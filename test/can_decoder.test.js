'use strict'

const assert = require('assert/strict')
const fs = require('fs')
const path = require('path')
const { parseCanLine, decodeFrame, createSnapshot } = require('../src/can_decoder')

const lines = fs.readFileSync(path.join(__dirname, '..', 'fixtures', 'can1-sample.log'), 'utf8').trim().split(/\r?\n/)

assert.equal(parseCanLine(lines[0]).id, 0x351)
assert.deepEqual(parseCanLine(lines[0]).data, [0x35, 0x02, 0x30, 0x02, 0xc4, 0x07, 0xe0, 0x01])
assert.equal(decodeFrame(parseCanLine(lines[0])).validated, false)

const now = 1754563202
const invalid = createSnapshot([
  { field: 'packVoltage', value: 52, timestamp: now },
  { field: 'packCurrent', value: 4, timestamp: now },
  { field: 'soc', value: 80, timestamp: now }
], now)
assert.equal(invalid.valid, false)
assert.equal(invalid.cellTelemetryValid, false)

const valid = createSnapshot([
  { field: 'packVoltage', value: 52, timestamp: now },
  { field: 'packCurrent', value: 4, timestamp: now },
  { field: 'soc', value: 80, timestamp: now },
  { field: 'vmax', value: 3.52, timestamp: now },
  { field: 'vmin', value: 3.49, timestamp: now },
  { field: 'maxCellIndex', value: 4, timestamp: now },
  { field: 'minCellIndex', value: 2, timestamp: now },
  { field: 'ccl', value: 0, timestamp: now }
], now)
assert.equal(valid.valid, true)
assert.equal(valid.ccl, 0)
assert.equal(valid.telemetryAge, 0)
