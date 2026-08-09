const current = flow.get('balancerRs485Snapshot') || {}
const lastValid = flow.get('balancerRs485LastValidSnapshot') || null
const number = (value, digits = 3) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
const bounded = (value, minimum, maximum, digits = 3) => typeof value === 'number' && Number.isFinite(value) && value >= minimum && value <= maximum ? value.toFixed(digits) : '—'
const count = (value) => typeof value === 'number' && Number.isInteger(value) && value >= 0 && value !== 65535 ? String(value) : '—'
const percent = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? value.toFixed(0) : '—'
const temperature = (value) => bounded(value, -40, 100, 1)
const identifier = (value, metric) => metric !== '—' && typeof value === 'number' && Number.isInteger(value) && value >= 0 && value !== 65535 ? String(value) : '—'
const byteHex = (value) => typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 255 ? `0x${value.toString(16).toUpperCase().padStart(2, '0')}` : '—'
const wordHex = (value) => typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 65535 ? `0x${value.toString(16).toUpperCase().padStart(4, '0')}` : '—'
const packedLocation = (value, metric, label) => {
    if (metric === '—' || typeof value !== 'number' || !Number.isInteger(value) || value < 0 || value > 65535 || value === 65535) return '—'
    const packed = value.toString(16).toUpperCase().padStart(4, '0')
    const cellByte = packed.slice(0, 2)
    const channel = /^\d{2}$/.test(cellByte) ? Number(cellByte) : Number.parseInt(cellByte, 16)
    const battery = Number.parseInt(packed.slice(2), 16)
    return channel > 0 && battery >= 0 ? `Battery ${battery} · ${label} ${String(channel).padStart(2, '0')}` : '—'
}
const cellLocation = (value, metric) => packedLocation(value, metric, 'Cell')
const sensorLocation = (value, metric) => packedLocation(value, metric, 'Sensor')
const onOff = (value) => typeof value === 'boolean' ? (value ? 'ON' : 'OFF') : '—'
const noneOr = (values, fallback = 'None') => Array.isArray(values) && values.length ? values.join(', ') : fallback
const status44View = (status) => {
    const raw = status || {}
    const status1 = raw.status1 || {}
    const status2 = raw.status2 || {}
    const status3 = raw.status3 || {}
    const status4 = raw.status4 || {}
    const status5 = raw.status5 || {}
    const alarms = raw.alarms || {}
    const ageMs = typeof raw.timestamp === 'number' ? Math.max(0, Date.now() - raw.timestamp) : null
    const available = raw.available === true && ageMs != null && ageMs <= 10000
    const protections = Array.isArray(status1.active) ? status1.active.map((item) => item.description || item.name) : []
    const cellAlarms = Array.isArray(alarms.cell) ? alarms.cell.map((value, index) => value ? `cell ${index + 1}: 0x${Number(value).toString(16).toUpperCase().padStart(2, '0')}` : null).filter(Boolean) : []
    const temperatureAlarms = Array.isArray(alarms.temperature) ? alarms.temperature.map((value, index) => value ? `temperature ${index + 1}: 0x${Number(value).toString(16).toUpperCase().padStart(2, '0')}` : null).filter(Boolean) : []
    const alarmParts = [...cellAlarms, ...temperatureAlarms]
    if (alarms.chargeCurrent) alarmParts.push(`charge current: 0x${Number(alarms.chargeCurrent).toString(16).toUpperCase().padStart(2, '0')}`)
    if (alarms.moduleVoltage) alarmParts.push(`module voltage: 0x${Number(alarms.moduleVoltage).toString(16).toUpperCase().padStart(2, '0')}`)
    if (alarms.dischargeCurrent) alarmParts.push(`discharge current: 0x${Number(alarms.dischargeCurrent).toString(16).toUpperCase().padStart(2, '0')}`)
    const fault = protections.length > 0 || alarmParts.length > 0 || (status4.cellFaults || []).length > 0 || (status5.cellFaults || []).length > 0
    const state = !available ? 'UNAVAILABLE' : fault ? 'WARN' : 'OK'
    const stateClass = !available ? 'neutral' : fault ? 'bad' : 'good'
    const chip = (label, value, tone) => ({ label, value, tone })
    const flag = (register, label, value, tone = value === 'ON' ? 'good' : 'neutral') => ({ register, label, value, tone })
    const bit = (value, position) => Boolean((Number(value) || 0) & (1 << position))
    const flags = available ? [
        flag('Status1', 'Pack under-voltage protection', onOff(bit(status1.raw, 7)), bit(status1.raw, 7) ? 'bad' : 'neutral'),
        flag('Status1', 'Charge-temperature protection', onOff(bit(status1.raw, 6)), bit(status1.raw, 6) ? 'bad' : 'neutral'),
        flag('Status1', 'Discharge-temperature protection', onOff(bit(status1.raw, 5)), bit(status1.raw, 5) ? 'bad' : 'neutral'),
        flag('Status1', 'Discharge over-current protection', onOff(bit(status1.raw, 4)), bit(status1.raw, 4) ? 'bad' : 'neutral'),
        flag('Status1', 'Charge over-current protection', onOff(bit(status1.raw, 2)), bit(status1.raw, 2) ? 'bad' : 'neutral'),
        flag('Status1', 'Cell under-voltage protection', onOff(bit(status1.raw, 1)), bit(status1.raw, 1) ? 'bad' : 'neutral'),
        flag('Status1', 'Over-voltage protection', onOff(bit(status1.raw, 0)), bit(status1.raw, 0) ? 'bad' : 'neutral'),
        flag('Status2', 'Precharge MOSFET', onOff(status2.prechargeMosfet)),
        flag('Status2', 'Charge MOSFET', onOff(status2.chargeMosfet)),
        flag('Status2', 'Discharge MOSFET', onOff(status2.dischargeMosfet)),
        flag('Status2', 'Module power active', onOff(status2.modulePowerActive)),
        flag('Status3', 'Effective charging', onOff(status3.effectiveCharging)),
        flag('Status3', 'Effective discharging', onOff(status3.effectiveDischarging)),
        flag('Status3', 'Heater active', onOff(status3.heaterActive), status3.heaterActive ? 'warning' : 'neutral'),
        flag('Status3', 'Fully charged', onOff(status3.fullyCharged), status3.fullyCharged ? 'warning' : 'neutral'),
        flag('Status3', 'Buzzer active', onOff(status3.buzzerActive), status3.buzzerActive ? 'warning' : 'neutral'),
        flag('Status4', 'Cell voltage-check faults, cells 1–8', noneOr(status4.cellFaults), (status4.cellFaults || []).length ? 'bad' : 'neutral'),
        flag('Status5', 'Cell voltage-check faults, cells 9–16', noneOr(status5.cellFaults), (status5.cellFaults || []).length ? 'bad' : 'neutral'),
        flag('Alarms', 'Active alarms', noneOr(alarmParts), alarmParts.length ? 'bad' : 'neutral'),
        flag('Reserved', 'Undefined/reserved bits', `S1 ${status1.reserved?.hex || '0x00'} · S2 ${status2.reserved?.hex || '0x00'} · S3 ${status3.reserved?.hex || '0x00'}`, 'neutral')
    ] : []
    return {
        available,
        state,
        stateClass,
        flags,
        error: raw.error || (ageMs != null && ageMs > 10000 ? `last status ${Math.round(ageMs / 1000)} s ago` : 'status not available'),
        raw: `S1 ${byteHex(status1.raw)} · S2 ${byteHex(status2.raw)} · S3 ${byteHex(status3.raw)} · S4 ${byteHex(status4.raw)} · S5 ${byteHex(status5.raw)}`,
        chips: available ? [
            chip('Charge MOSFET', onOff(status2.chargeMosfet), status2.chargeMosfet ? 'good' : 'neutral'),
            chip('Discharge MOSFET', onOff(status2.dischargeMosfet), status2.dischargeMosfet ? 'good' : 'neutral'),
            chip('Module power', onOff(status2.modulePowerActive), status2.modulePowerActive ? 'good' : 'neutral'),
            chip('Effective charge/discharge', `${onOff(status3.effectiveCharging)}/${onOff(status3.effectiveDischarging)}`, status3.effectiveCharging || status3.effectiveDischarging ? 'good' : 'neutral'),
            ...(status2.prechargeMosfet ? [chip('Precharge', 'ON', 'warning')] : []),
            ...(status3.heaterActive ? [chip('Heater', 'ON', 'warning')] : [])
        ] : [],
        protection: noneOr(protections),
        hardware: available ? `Precharge MOSFET ${onOff(status2.prechargeMosfet)} · Charge MOSFET ${onOff(status2.chargeMosfet)} · Discharge MOSFET ${onOff(status2.dischargeMosfet)} · Module power ${onOff(status2.modulePowerActive)}` : '—',
        effective: available ? `Effective charging ${onOff(status3.effectiveCharging)} · Effective discharging ${onOff(status3.effectiveDischarging)} · Heater active ${onOff(status3.heaterActive)} · Fully charged ${onOff(status3.fullyCharged)} · Buzzer active ${onOff(status3.buzzerActive)}` : '—',
        status4Faults: available ? noneOr(status4.cellFaults) : '—',
        status5Faults: available ? noneOr(status5.cellFaults) : '—',
        cellFaults: available ? `Status4, cells 1–8: ${noneOr(status4.cellFaults)} · Status5, cells 9–16: ${noneOr(status5.cellFaults)}` : '—',
        alarms: noneOr(alarmParts),
        reserved: available ? `S1 ${status1.reserved?.hex || '0x00'} · S2 ${status2.reserved?.hex || '0x00'} · S3 ${status3.reserved?.hex || '0x00'}` : '—'
    }
}
const now = Date.now()
const lastValidAgeMs = lastValid && typeof lastValid.timestamp === 'number' ? Math.max(0, now - lastValid.timestamp) : null
const displaySnapshot = current.valid === true ? current : lastValidAgeMs != null && lastValidAgeMs <= 10000 ? lastValid : {}
const displayState = current.valid === true ? 'LIVE' : displaySnapshot.timestamp ? 'STALE' : 'INVALID'
const aggregate = displaySnapshot.aggregate || {}
const system = displaySnapshot.system || {}
const limits = displaySnapshot.limits || {}
const discovery = current.discovery || displaySnapshot.discovery || {}
const inventory = current.inventory || displaySnapshot.inventory || {}
const health = current.serialHealth || displaySnapshot.serialHealth || {}
const systemMaximumCellVoltage = bounded(system.maximumCellVoltage61, 2, 4.5)
const systemMinimumCellVoltage = bounded(system.minimumCellVoltage61, 2, 4.5)
const bmsAverageTemperature = temperature(system.averageBmsTemperature61)
const bmsMaximumTemperature = temperature(system.maximumBmsTemperature61)
const bmsMinimumTemperature = temperature(system.minimumBmsTemperature61)
const bmsMaximumId = sensorLocation(system.maximumBmsTemperatureId61, bmsMaximumTemperature)
const bmsMinimumId = sensorLocation(system.minimumBmsTemperatureId61, bmsMinimumTemperature)
const bmsSingleTemperature = bmsMaximumTemperature !== '—' && bmsMaximumTemperature === bmsMinimumTemperature && bmsMaximumId === bmsMinimumId
    ? { value: bmsMaximumTemperature, id: bmsMaximumId }
    : null

const batteries = (displaySnapshot.batteries || []).map((battery) => {
    const cells = battery.effectiveCells || battery.effective_cells || []
    const cellValues = cells.map((cell) => cell.voltage).filter((value) => typeof value === 'number' && Number.isFinite(value))
    const completeCellArray = battery.valid === true && cellValues.length === 16
    const batteryMinimumCellVoltage = completeCellArray ? Math.min(...cellValues) : null
    const unbalancedCellCount = completeCellArray
        ? cellValues.filter((value) => value - batteryMinimumCellVoltage >= 0.030 - Number.EPSILON).length
        : null
    return {
        address: battery.address,
        valid: battery.valid === true,
        voltage: number(battery.voltage),
        current: number(battery.current, 1),
        cycleCount: count(battery.cycleCount ?? battery.cycle_count),
        remainingCapacity: bounded(battery.remainingCapacityAh ?? battery.remaining_capacity_ah, 0, 100000, 1),
        totalCapacity: bounded(battery.totalCapacityAh ?? battery.total_capacity_ah, 0, 100000, 1),
        capacitySoc: bounded(battery.capacitySoc ?? battery.capacity_soc, 0, 100, 1),
        delta: number(battery.systemVoltageDeltaMv ?? battery.system_voltage_delta_mv, 1),
        cellSpread: cellValues.length ? number(Math.max(...cellValues) - Math.min(...cellValues)) : '—',
        cellExtremaSource: 'CID2 42 cell array',
        unbalancedCellCount: unbalancedCellCount == null ? '—' : String(unbalancedCellCount),
        cells: cells.map((cell) => ({ index: cell.index, voltage: number(cell.voltage) })),
        temperatures: (battery.temperatures || []).map((value, index) => ({ index: index + 1, value: number(value, 1) })),
        minimumTemperature: number(battery.minimumTemperature ?? battery.minimum_temperature, 1),
        maximumTemperature: number(battery.maximumTemperature ?? battery.maximum_temperature, 1),
        averageTemperature: number(battery.averageTemperature ?? battery.average_temperature, 1),
        reportedSum: number(battery.directlyReportedCellSum ?? battery.directly_reported_cell_sum),
        reconstructedSum: number(battery.reconstructedCellSum ?? battery.reconstructed_cell_sum),
        voltageDelta: number(battery.reconstructedVoltageDeltaMv ?? battery.reconstructed_voltage_delta_mv, 1),
        errors: battery.validationErrors || battery.validation_errors || [],
        status44: status44View(battery.status44)
    }
})

const completeBatteries = batteries.filter((battery) => battery.unbalancedCellCount !== '—')
const totalUnbalancedCellCount = completeBatteries.length
    ? String(completeBatteries.reduce((sum, battery) => sum + Number(battery.unbalancedCellCount), 0))
    : '—'

const sourceBatteries = displaySnapshot.batteries || []
const completeCapacity = sourceBatteries.length > 0 && sourceBatteries.every((battery) =>
    battery.valid === true &&
    Number.isInteger(battery.cycleCount ?? battery.cycle_count) &&
    Number.isFinite(battery.remainingCapacityAh ?? battery.remaining_capacity_ah) &&
    Number.isFinite(battery.totalCapacityAh ?? battery.total_capacity_ah)
)
const cycleCounts = completeCapacity ? sourceBatteries.map((battery) => battery.cycleCount ?? battery.cycle_count) : []
const commonCycleCount = cycleCounts.length && cycleCounts.every((value) => value === cycleCounts[0])
    ? String(cycleCounts[0])
    : completeCapacity ? 'varies' : '—'
const totalRemainingCapacity = completeCapacity
    ? number(sourceBatteries.reduce((sum, battery) => sum + (battery.remainingCapacityAh ?? battery.remaining_capacity_ah), 0), 1)
    : '—'
const totalCapacity = completeCapacity
    ? number(sourceBatteries.reduce((sum, battery) => sum + (battery.totalCapacityAh ?? battery.total_capacity_ah), 0), 1)
    : '—'
const cellTemperatureValues = sourceBatteries
    .flatMap((battery) => Array.isArray(battery.temperatures) ? battery.temperatures : [])
    .filter((value) => typeof value === 'number' && Number.isFinite(value) && value >= -40 && value <= 100)
const systemCellTemperatureAverage = temperature(system.averageCellTemperature61) !== '—'
    ? temperature(system.averageCellTemperature61)
    : cellTemperatureValues.length
        ? number(cellTemperatureValues.reduce((sum, value) => sum + value, 0) / cellTemperatureValues.length, 1)
        : '—'

msg.payload = {
    decoder: current.decoderHealth || 'unknown',
    valid: current.valid === true,
    displayState,
    displayAgeMs: lastValidAgeMs,
    age: displaySnapshot.timestamp == null ? '—' : `${Math.max(0, now - displaySnapshot.timestamp)} ms`,
    reason: current.reason || null,
    port: current.serialPort || displaySnapshot.serialPort || '—',
    baud: current.baud || displaySnapshot.baud || '—',
    serialHealth: {
        state: health.state || 'unknown',
        ownerConflict: health.ownerConflict === true,
        reconnectCount: health.reconnectCount || 0,
        lastPollDurationMs: health.lastPollDurationMs == null ? '—' : number(health.lastPollDurationMs, 1),
        lastError: health.lastError || current.reason || 'none',
        lastErrorType: health.lastErrorType || 'none'
    },
    system: {
        voltage: bounded(system.voltage61, 40, 70),
        current: bounded(system.current61, -1000, 1000, 1),
        soc: percent(system.soc61),
        averageCycleCount: count(system.averageCycleCount61),
        maximumCycleCount: count(system.maximumCycleCount61),
        averageSoh: percent(system.averageSoh61),
        minimumSoh: percent(system.minimumSoh61),
        cycleCount: commonCycleCount,
        remainingCapacity: totalRemainingCapacity,
        totalCapacity,
        cellSummary: {
            maximumVoltage: systemMaximumCellVoltage,
            maximumId: cellLocation(system.maximumCellId61, systemMaximumCellVoltage),
            maximumRawId: wordHex(system.maximumCellId61),
            minimumVoltage: systemMinimumCellVoltage,
            minimumId: cellLocation(system.minimumCellId61, systemMinimumCellVoltage),
            minimumRawId: wordHex(system.minimumCellId61),
            spread: number(aggregate.spread)
        },
        cellTemperature: {
            average: systemCellTemperatureAverage,
            maximum: temperature(system.maximumCellTemperature61),
            maximumId: sensorLocation(system.maximumCellTemperatureId61, temperature(system.maximumCellTemperature61)),
            maximumRawId: wordHex(system.maximumCellTemperatureId61),
            minimum: temperature(system.minimumCellTemperature61),
            minimumId: sensorLocation(system.minimumCellTemperatureId61, temperature(system.minimumCellTemperature61)),
            minimumRawId: wordHex(system.minimumCellTemperatureId61)
        },
        mosfetTemperature: {
            average: temperature(system.averageMosfetTemperature61),
            maximum: temperature(system.maximumMosfetTemperature61),
            maximumId: sensorLocation(system.maximumMosfetTemperatureId61, temperature(system.maximumMosfetTemperature61)),
            maximumRawId: wordHex(system.maximumMosfetTemperatureId61),
            minimum: temperature(system.minimumMosfetTemperature61),
            minimumId: sensorLocation(system.minimumMosfetTemperatureId61, temperature(system.minimumMosfetTemperature61)),
            minimumRawId: wordHex(system.minimumMosfetTemperatureId61)
        },
        bmsTemperature: {
            average: bmsAverageTemperature,
            maximum: bmsMaximumTemperature,
            maximumId: bmsMaximumId,
            maximumRawId: wordHex(system.maximumBmsTemperatureId61),
            minimum: bmsMinimumTemperature,
            minimumId: bmsMinimumId,
            minimumRawId: wordHex(system.minimumBmsTemperatureId61),
            single: bmsSingleTemperature
        }
    },
    limits: {
        chargeVoltage: number(limits.chargeVoltage),
        dischargeVoltage: number(limits.dischargeVoltage),
        ccl: number(limits.chargeCurrent, 1),
        dcl: number(limits.dischargeCurrentSigned, 1),
        status: limits.statusRaw == null ? '—' : `0x${Number(limits.statusRaw).toString(16).toUpperCase().padStart(2, '0')}`,
        statusState: limits.statusFlags?.state || 'unknown',
        statusActive: Array.isArray(limits.statusFlags?.active)
            ? limits.statusFlags.active.map((item) => item.description || item.name)
            : [],
        statusFlags: limits.statusFlags || {
            chargeEnabled: null,
            dischargeEnabled: null,
            strongCharge: null,
            fullCharge: null,
            unknownReservedBits: null
        }
    },
    discovery: {
        count: discovery.respondingCount || 0,
        addresses: (discovery.respondingAddresses || []).join(', ') || '—'
    },
    aggregate: {
        source: aggregate.source === 'CID2_61_SYSTEM_SUMMARY' ? 'CID2 61 system summary' : '—',
        vmin: number(aggregate.vmin),
        vmax: number(aggregate.vmax),
        spread: number((aggregate.spread || 0) * 1000, 1),
        unbalancedCellCount: totalUnbalancedCellCount,
        min: `${aggregate.minCellAddress ?? '—'} / cell ${aggregate.minCellIndex ?? '—'}`,
        max: `${aggregate.maxCellAddress ?? '—'} / cell ${aggregate.maxCellIndex ?? '—'}`,
        current: number(aggregate.summedBatteryCurrent, 1),
        temperature: `${number(aggregate.minimumTemperature, 1)}–${number(aggregate.maximumTemperature, 1)} °C`
    },
    inventory: {
        activeAddresses: inventory.activeAddresses || discovery.activeAddresses || [],
        pendingRemoval: (inventory.pendingRemoval || discovery.pendingRemoval || []).map((item) => ({
            address: item.address,
            missedScans: item.missedScans,
            maxAttempts: item.maxAttempts || 10,
            lastSeenAt: item.lastSeenAt
        })),
        mode: inventory.discoveryMode || 'normal',
        scanIntervalSeconds: inventory.scanIntervalSeconds || 60,
        nextDiscoveryInSeconds: inventory.nextDiscoveryInSeconds
    },
    batteries
}
return msg
