'use strict'

function finite (value) {
  return typeof value === 'number' && Number.isFinite(value)
}

function parseCanLine (line) {
  const match = String(line).trim().match(/^\(([^)]+)\)\s+(\S+)\s+([0-9A-Fa-f]+)#([0-9A-Fa-f]*)$/)
  if (!match) return null
  const data = match[4].match(/../g) || []
  return {
    timestamp: Number(match[1]),
    interface: match[2],
    id: Number.parseInt(match[3], 16),
    dlc: data.length,
    data: data.map(byte => Number.parseInt(byte, 16))
  }
}

function frameKey (id) {
  return `0x${id.toString(16).toUpperCase()}`
}

function decodeFrame (frame, mapping = {}) {
  if (!frame || !Number.isInteger(frame.id) || !Array.isArray(frame.data)) return null
  const configured = mapping.frames && mapping.frames[frameKey(frame.id)]
  const decoded = configured && configured.validated === true
    ? { ...configured.decode(frame.data), validated: true }
    : { validated: false }
  return { ...frame, frame: frameKey(frame.id), ...decoded }
}

function createSnapshot (samples, now, staleMs = 5000) {
  const result = {
    timestamp: now,
    source: 'direct-can',
    packVoltage: null,
    packCurrent: null,
    soc: null,
    vmax: null,
    vmin: null,
    maxCellIndex: null,
    minCellIndex: null,
    ccl: null,
    dcl: null,
    cvl: null,
    bmsStatus: null,
    telemetryAge: null,
    valid: false,
    cellTelemetryValid: false
  }
  for (const sample of samples || []) {
    if (!sample || !finite(sample.value) || !finite(sample.timestamp)) continue
    if (now - sample.timestamp > staleMs) continue
    if (Object.prototype.hasOwnProperty.call(result, sample.field)) result[sample.field] = sample.value
    if (sample.field === 'bmsStatus') result.bmsStatus = sample.value
  }
  const timestamps = (samples || []).map(sample => sample && sample.timestamp).filter(finite)
  result.telemetryAge = timestamps.length ? Math.max(0, now - Math.min(...timestamps)) : null
  result.cellTelemetryValid = finite(result.vmax) && finite(result.vmin) && result.vmax >= result.vmin
  result.valid = finite(result.packVoltage) && finite(result.packCurrent) && finite(result.soc) && result.cellTelemetryValid
  return result
}

module.exports = { parseCanLine, decodeFrame, createSnapshot, frameKey }
