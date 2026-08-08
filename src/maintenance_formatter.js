const current = flow.get('balancerRs485Snapshot') || {}
const lastValid = flow.get('balancerRs485LastValidSnapshot') || null
const number = (value, digits = 3) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
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
        errors: battery.validationErrors || battery.validation_errors || []
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
    system: { voltage: number(system.voltage61), soc: number(system.soc61, 1) },
    limits: {
        chargeVoltage: number(limits.chargeVoltage),
        dischargeVoltage: number(limits.dischargeVoltage),
        ccl: number(limits.chargeCurrent, 1),
        dcl: number(limits.dischargeCurrentSigned, 1),
        status: limits.statusRaw == null ? '—' : `0x${Number(limits.statusRaw).toString(16).toUpperCase()}`
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
