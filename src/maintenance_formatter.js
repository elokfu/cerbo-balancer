const current = flow.get('balancerRs485Snapshot') || {}
const lastValid = flow.get('balancerRs485LastValidSnapshot') || null
const number = (value, digits = 3) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
const bounded = (value, minimum, maximum, digits = 3) => typeof value === 'number' && Number.isFinite(value) && value >= minimum && value <= maximum ? value.toFixed(digits) : '—'
const count = (value) => typeof value === 'number' && Number.isInteger(value) && value >= 0 && value !== 65535 ? String(value) : '—'
const percent = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? value.toFixed(0) : '—'
const temperature = (value) => bounded(value, -40, 100, 1)
const identifier = (value, metric) => metric !== '—' && typeof value === 'number' && Number.isInteger(value) && value >= 0 && value !== 65535 ? String(value) : '—'
const byteHex = (value) => typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= 255 ? `0x${value.toString(16).toUpperCase().padStart(2, '0')}` : '—'
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
    return {
        available,
        state,
        stateClass,
        error: raw.error || (ageMs != null && ageMs > 10000 ? `last status ${Math.round(ageMs / 1000)} s ago` : 'status not available'),
        raw: `S1 ${byteHex(status1.raw)} · S2 ${byteHex(status2.raw)} · S3 ${byteHex(status3.raw)} · S4 ${byteHex(status4.raw)} · S5 ${byteHex(status5.raw)}`,
        chips: available ? [
            chip('Charge MOS', onOff(status2.chargeMosfet), status2.chargeMosfet ? 'good' : 'neutral'),
            chip('Discharge MOS', onOff(status2.dischargeMosfet), status2.dischargeMosfet ? 'good' : 'neutral'),
            chip('Power', onOff(status2.modulePowerActive), status2.modulePowerActive ? 'good' : 'neutral'),
            chip('Effective', `${onOff(status3.effectiveCharging)}/${onOff(status3.effectiveDischarging)}`, status3.effectiveCharging || status3.effectiveDischarging ? 'good' : 'neutral'),
            ...(status2.prechargeMosfet ? [chip('Precharge', 'ON', 'warning')] : []),
            ...(status3.heaterActive ? [chip('Heater', 'ON', 'warning')] : [])
        ] : [],
        protection: noneOr(protections),
        hardware: available ? `Precharge ${onOff(status2.prechargeMosfet)} · Charge MOS ${onOff(status2.chargeMosfet)} · Discharge MOS ${onOff(status2.dischargeMosfet)} · Power ${onOff(status2.modulePowerActive)}` : '—',
        effective: available ? `Charging ${onOff(status3.effectiveCharging)} · Discharging ${onOff(status3.effectiveDischarging)} · Heater ${onOff(status3.heaterActive)} · Full ${onOff(status3.fullyCharged)} · Buzzer ${onOff(status3.buzzerActive)}` : '—',
        cellFaults: available ? `Cells 1–8: ${noneOr(status4.cellFaults)} · Cells 9–16: ${noneOr(status5.cellFaults)}` : '—',
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
const bmsMaximumId = identifier(system.maximumBmsTemperatureId61, bmsMaximumTemperature)
const bmsMinimumId = identifier(system.minimumBmsTemperatureId61, bmsMinimumTemperature)
const bmsSingleTemperature = bmsMaximumTemperature !== '—' && bmsMaximumTemperature === bmsMinimumTemperature && bmsMaximumId === bmsMinimumId
    ? { value: bmsMaximumTemperature, id: bmsMaximumId }
    : null

const batteries = (displaySnapshot.batteries || []).map((battery) => {
    const cells = battery.effectiveCells || battery.effective_cells || []
    const cellValues = cells.map((cell) => cell.voltage).filter((value) => typeof value === 'number' && Number.isFinite(value))
    return {
        address: battery.address,
        valid: battery.valid === true,
        voltage: number(battery.voltage),
        current: number(battery.current, 1),
        delta: number(battery.systemVoltageDeltaMv ?? battery.system_voltage_delta_mv, 1),
        cellSpread: cellValues.length ? number(Math.max(...cellValues) - Math.min(...cellValues)) : '—',
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
        cellSummary: {
            maximumVoltage: systemMaximumCellVoltage,
            maximumId: identifier(system.maximumCellId61, systemMaximumCellVoltage),
            minimumVoltage: systemMinimumCellVoltage,
            minimumId: identifier(system.minimumCellId61, systemMinimumCellVoltage),
            spread: systemMaximumCellVoltage !== '—' && systemMinimumCellVoltage !== '—'
                ? number(Number(systemMaximumCellVoltage) - Number(systemMinimumCellVoltage), 3)
                : '—'
        },
        cellTemperature: {
            average: temperature(system.averageCellTemperature61),
            maximum: temperature(system.maximumCellTemperature61),
            maximumId: identifier(system.maximumCellTemperatureId61, temperature(system.maximumCellTemperature61)),
            minimum: temperature(system.minimumCellTemperature61),
            minimumId: identifier(system.minimumCellTemperatureId61, temperature(system.minimumCellTemperature61))
        },
        mosfetTemperature: {
            average: temperature(system.averageMosfetTemperature61),
            maximum: temperature(system.maximumMosfetTemperature61),
            maximumId: identifier(system.maximumMosfetTemperatureId61, temperature(system.maximumMosfetTemperature61)),
            minimum: temperature(system.minimumMosfetTemperature61),
            minimumId: identifier(system.minimumMosfetTemperatureId61, temperature(system.minimumMosfetTemperature61))
        },
        bmsTemperature: {
            average: bmsAverageTemperature,
            maximum: bmsMaximumTemperature,
            maximumId: bmsMaximumId,
            minimum: bmsMinimumTemperature,
            minimumId: bmsMinimumId,
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
        vmin: number(aggregate.vmin),
        vmax: number(aggregate.vmax),
        spread: number((aggregate.spread || 0) * 1000, 1),
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
