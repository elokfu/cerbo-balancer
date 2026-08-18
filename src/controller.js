'use strict'

const STATES = Object.freeze({
  NORMAL: 'NORMAL',
  BALANCING: 'BALANCING',
  SAFETY_STOP: 'SAFETY_STOP'
})

const CONTROLLER_VERSION = 6

const FLOAT_CELL_THRESHOLD = 3.5
const FLOAT_DEFAULT_VOLTAGE = 56.5
const BALANCE_EXIT_CURRENT_THRESHOLD = 1.5

const DEFAULT_CONFIG = Object.freeze({
  balancingVoltageCeiling: 56.5,
  safetyFallbackVoltage: 55.0,
  safetyFallbackCurrent: 10.0,
  balancerSpreadThreshold: 0.030,
  balanceBatteryCurrentTarget: 2.0,
  currentTargetTolerance: 0.25,
  feedForwardAlpha: 0.20,
  shareMinimumSelectedCurrent: 0.25,
  shareMinimumTotalCurrent: 0.5,
  feedForwardFallbackCurrent: 2.0,
  feedForwardGain: 1.0,
  kp: 0.20,
  ki: 0.02,
  maxIntegralTerm: 10.0,
  aggregateCurrentMinimum: 0.0,
  aggregateCurrentMaximum: 100.0,
  currentIncreaseRatePerMin: 10.0,
  solarShortfallTolerance: 2.0,
  solarDetectionSamples: 4,
  qualificationSamples: 8,
  permissionRearmDebounceMs: 5000,
  telemetryStaleMs: 20000
})

function finite (value) { return typeof value === 'number' && Number.isFinite(value) }
function clamp (value, minimum, maximum) { return Math.max(minimum, Math.min(maximum, value)) }

function emptyState () {
  return {
    version: CONTROLLER_VERSION,
    state: STATES.NORMAL,
    automaticBalancingEnabled: true,
    selectedAddress: null,
    selectedReason: null,
    completionLatched: false,
    permissionOffSeen: false,
    permissionOnStartedAt: null,
    integralTerm: 0,
    aggregateCurrentCommand: DEFAULT_CONFIG.feedForwardFallbackCurrent,
    rawSelectedShare: null,
    filteredSelectedShare: null,
    feedForwardCurrent: DEFAULT_CONFIG.feedForwardFallbackCurrent,
    effectiveFeedForwardCurrent: DEFAULT_CONFIG.feedForwardFallbackCurrent,
    pTerm: 0,
    solarLimited: false,
    solarDeficitSamples: 0,
    solarRecoverySamples: 0,
    entryQualificationSamples: {},
    exitQualificationSamples: 0,
    lastQualificationTelemetryTimestamp: null,
    floatVoltage: FLOAT_DEFAULT_VOLTAGE,
    floatQualified: false,
    floatQualifiedAt: null,
    floatSampleCount: 0,
    floatVoltageSum: 0,
    lastControlSampleTimestamp: null,
    lastTransitionAt: null,
    lastStopReason: null,
    fault: null,
    lastEvent: null,
    csvLogging: { enabled: false, filename: null }
  }
}

function validateConfig (candidate = {}) {
  const config = { ...DEFAULT_CONFIG, ...candidate }
  // Normal limits are owned by the Cerbo Charge Control UI, not this
  // controller. Drop the former persisted override during migration.
  delete config.normalPackVoltageMax
  // Old persisted values must not shorten the new eight-second telemetry budget.
  config.telemetryStaleMs = Math.max(DEFAULT_CONFIG.telemetryStaleMs, config.telemetryStaleMs)
  const errors = []
  const positive = [
    'balancingVoltageCeiling', 'safetyFallbackVoltage',
    'safetyFallbackCurrent', 'balancerSpreadThreshold', 'balanceBatteryCurrentTarget',
    'currentTargetTolerance', 'feedForwardAlpha', 'shareMinimumSelectedCurrent',
    'shareMinimumTotalCurrent', 'feedForwardFallbackCurrent', 'kp', 'ki',
    'maxIntegralTerm', 'aggregateCurrentMaximum', 'currentIncreaseRatePerMin',
    'solarShortfallTolerance', 'solarDetectionSamples', 'qualificationSamples', 'permissionRearmDebounceMs',
    'telemetryStaleMs'
  ]
  for (const field of positive) {
    if (!finite(config[field]) || config[field] <= 0) errors.push(`${field} must be positive`)
  }
  if (!finite(config.aggregateCurrentMinimum) || config.aggregateCurrentMinimum < 0) {
    errors.push('aggregateCurrentMinimum must be non-negative')
  }
  if (!(config.aggregateCurrentMinimum <= config.aggregateCurrentMaximum)) {
    errors.push('aggregate current bounds are invalid')
  }
  if (!(config.feedForwardAlpha > 0 && config.feedForwardAlpha <= 1)) {
    errors.push('feedForwardAlpha must be greater than zero and at most one')
  }
  if (!finite(config.feedForwardGain) || config.feedForwardGain < 0 || config.feedForwardGain > 1) {
    errors.push('feedForwardGain must be between zero and one')
  }
  if (!Number.isInteger(config.solarDetectionSamples)) {
    errors.push('solarDetectionSamples must be an integer')
  }
  if (!Number.isInteger(config.qualificationSamples)) {
    errors.push('qualificationSamples must be an integer')
  }
  return { valid: errors.length === 0, errors, config }
}

function createBalancerController (options = {}) {
  const now = options.now || (() => Date.now())
  const localDate = options.localDate || (timestamp => new Date(timestamp).toISOString().slice(0, 10))
  let config = { ...DEFAULT_CONFIG }
  let state = emptyState()
  let telemetry = null
  let configurationResult = null
  const events = []
  const history = []
  let lastPersistenceKey = null
  let pendingCsvLogging = null

  function record (timestamp, type, detail, extra = {}) {
    const event = { timestamp, date: localDate(timestamp), type, detail, ...extra }
    events.push(event)
    state.lastEvent = event
    return event
  }

  function transition (next, timestamp, reason) {
    if (state.state === next) return
    record(timestamp, 'state_transition', `${state.state} -> ${next}: ${reason}`)
    state.state = next
    state.lastTransitionAt = timestamp
  }

  function resetControl () {
    state.integralTerm = 0
    state.aggregateCurrentCommand = config.feedForwardFallbackCurrent
    state.rawSelectedShare = null
    state.filteredSelectedShare = null
    state.feedForwardCurrent = config.feedForwardFallbackCurrent
    state.effectiveFeedForwardCurrent = config.feedForwardGain * config.feedForwardFallbackCurrent
    state.pTerm = 0
    state.solarLimited = false
    state.solarDeficitSamples = 0
    state.solarRecoverySamples = 0
    state.lastControlSampleTimestamp = null
  }

  function releaseSelection () {
    state.selectedAddress = null
    state.selectedReason = null
    state.exitQualificationSamples = 0
  }

  function migrateState (persisted = {}) {
    const migrated = { ...emptyState() }
    if (typeof persisted.automaticBalancingEnabled === 'boolean') {
      migrated.automaticBalancingEnabled = persisted.automaticBalancingEnabled
    } else if (typeof persisted.enabled === 'boolean') {
      migrated.automaticBalancingEnabled = persisted.enabled
    }
    migrated.csvLogging = persisted.csvLogging || migrated.csvLogging
    const compatibleVersions = [3, 4, 5, CONTROLLER_VERSION]
    migrated.completionLatched = compatibleVersions.includes(persisted.version) && persisted.completionLatched === true
    migrated.permissionOffSeen = compatibleVersions.includes(persisted.version) && persisted.permissionOffSeen === true
    if (compatibleVersions.includes(persisted.version) && Object.values(STATES).includes(persisted.state)) {
      const { enabled, autoManualMode, mode, ...current } = persisted
      Object.assign(migrated, current)
      migrated.version = CONTROLLER_VERSION
      migrated.entryQualificationSamples = {}
      migrated.exitQualificationSamples = 0
      migrated.lastQualificationTelemetryTimestamp = null
      migrated.floatSampleCount = 0
      migrated.floatVoltageSum = 0
      if (!finite(migrated.floatVoltage) || migrated.floatVoltage <= 0) migrated.floatVoltage = FLOAT_DEFAULT_VOLTAGE
      if (persisted.version !== CONTROLLER_VERSION) {
        migrated.floatVoltage = FLOAT_DEFAULT_VOLTAGE
        migrated.floatQualified = false
        migrated.floatQualifiedAt = null
      }
      if (typeof persisted.automaticBalancingEnabled !== 'boolean' && typeof enabled === 'boolean') {
        migrated.automaticBalancingEnabled = enabled
      }
      return migrated
    }
    const oldActive = [
      'BALANCE_ELIGIBILITY', 'BALANCE_RESTART', 'BALANCE_CURRENT_CONTROL',
      'BALANCE_CONFIRM'
    ]
    const oldFault = ['FAULT_OR_ABORT', 'PROTECTION_RECOVERY']
    if (oldActive.includes(persisted.state) && Number.isInteger(persisted.selectedAddress)) {
      migrated.state = STATES.BALANCING
      migrated.selectedAddress = persisted.selectedAddress
      migrated.selectedReason = 'migrated selected battery'
    } else if (oldFault.includes(persisted.state)) {
      migrated.state = STATES.SAFETY_STOP
      migrated.fault = persisted.fault || 'migrated controller fault'
    }
    return migrated
  }

  function expectedAddresses () {
    if (!telemetry) return []
    const values = telemetry.expectedBatteries || telemetry.expectedAddresses || []
    return Array.isArray(values) ? values.map(Number).filter(Number.isInteger).sort((a, b) => a - b) : []
  }

  function batteries () {
    return telemetry && Array.isArray(telemetry.batteries) ? telemetry.batteries : []
  }

  function batteryByAddress (address) {
    return batteries().find(battery => battery.address === address) || null
  }

  function localProtection (battery) {
    const active = battery && battery.status44 && battery.status44.status1 && battery.status44.status1.active
    return Array.isArray(active) && active.length > 0
  }

  function chargeMosfetOn (battery) {
    const status = battery && battery.status44 && battery.status44.status2
    return status ? status.chargeMosfet === true : battery && battery.chargeMosfet === true
  }

  function effectiveDischarging (battery) {
    const status = battery && battery.status44 && battery.status44.status3
    return Boolean(status && status.effectiveDischarging === true)
  }

  function masterSoc () {
    const soc = telemetry && telemetry.system && telemetry.system.soc61
    return Number.isInteger(soc) && soc >= 0 && soc <= 100 ? soc : null
  }

  function batterySoc (battery) {
    return battery && Number.isInteger(battery.soc) ? battery.soc : masterSoc()
  }

  function battery61Valid (battery) {
    return Boolean(battery && battery.valid === true &&
      finite(battery.vmin) && finite(battery.vmax) && finite(battery.spread) &&
      battery.vmax >= battery.vmin && finite(battery.current))
  }

  function freshTelemetry (timestamp) {
    const age = telemetry && finite(telemetry.timestamp) ? Math.max(0, timestamp - telemetry.timestamp) : Infinity
    const expected = expectedAddresses()
    const complete = expected.length > 0 && expected.every(address => battery61Valid(batteryByAddress(address)))
    const inventoryComplete = expected.length > 0 && !((telemetry && telemetry.inventory && telemetry.inventory.pendingRemoval) || []).length
    const valid = Boolean(telemetry && telemetry.valid === true && age <= config.telemetryStaleMs && complete && inventoryComplete)
    let lockout = null
    if (!telemetry) lockout = 'RS485 telemetry is unavailable'
    else if (age > config.telemetryStaleMs) lockout = 'RS485 telemetry is stale'
    else if (!inventoryComplete) lockout = 'complete fresh telemetry from every expected battery is required'
    else if (!complete) lockout = 'addressed CID2 61 telemetry is incomplete or invalid'
    else if (!telemetry.valid) lockout = telemetry.reason || 'RS485 telemetry is invalid'
    return { age, valid, lockout }
  }

  function limits () { return telemetry && telemetry.limits ? telemetry.limits : {} }
  function chargeControlSettings () {
    return telemetry && telemetry.chargeControlSettings ? telemetry.chargeControlSettings : {}
  }
  function normalChargeVoltage () {
    const settings = chargeControlSettings()
    const candidates = [{ source: 'HARD_CEILING', value: config.balancingVoltageCeiling }]
    if (finite(limits().chargeVoltage)) candidates.push({ source: 'BMS_CVL', value: limits().chargeVoltage })
    if (settings.voltageLimitEnabled === true && finite(settings.maxChargeVoltage)) candidates.push({ source: 'CERBO_UI_CVL', value: settings.maxChargeVoltage })
    if (floatCvlActive()) candidates.push({ source: state.floatQualified ? 'LEARNED_FLOAT' : 'DEFAULT_FLOAT', value: state.floatVoltage })
    return candidates.reduce((lowest, candidate) => candidate.value < lowest.value ? candidate : lowest).value
  }
  function socCurrentCap () {
    const soc = masterSoc()
    if (soc === 97) return 50
    if (soc === 98) return 20
    if (soc >= 99) return 10
    return null
  }
  function normalCurrentPolicy () {
    const settings = chargeControlSettings()
    const candidates = []
    if (finite(limits().chargeCurrent)) candidates.push({ source: 'BMS_CCL', value: Math.max(0, limits().chargeCurrent) })
    if (settings.currentLimitEnabled === true && finite(settings.maxChargeCurrent)) candidates.push({ source: 'CERBO_UI_CCL', value: Math.max(0, settings.maxChargeCurrent) })
    const cap = socCurrentCap()
    if (finite(cap)) candidates.push({ source: 'SOC_DERATING', value: cap })
    if (!candidates.length) candidates.push({ source: 'SAFETY_FALLBACK', value: config.safetyFallbackCurrent })
    const limiting = candidates.reduce((lowest, candidate) => candidate.value < lowest.value ? candidate : lowest)
    return { value: limiting.value, source: limiting.source, socCap: cap }
  }
  function normalChargeCurrent () { return normalCurrentPolicy().value }
  function floatCvlActive () { return state.completionLatched && masterSoc() === 100 }
  function chargePermitted () { return limits().statusFlags && limits().statusFlags.chargeEnabled === true }
  function ccl () { return finite(telemetry && telemetry.ccl) ? Math.max(0, telemetry.ccl) : null }
  function virtualBmsSelected () { return Boolean(telemetry && telemetry.activeBms && telemetry.activeBms.virtualSelected === true) }

  function eligible (battery) {
    return Boolean(battery61Valid(battery) && battery.spread > config.balancerSpreadThreshold &&
      chargeMosfetOn(battery) && !localProtection(battery))
  }

  function resetIncompleteQualifications () {
    state.entryQualificationSamples = {}
    state.exitQualificationSamples = 0
    state.floatSampleCount = 0
    state.floatVoltageSum = 0
  }

  function updateFloatVoltageSample (timestamp) {
    const system = telemetry && telemetry.system ? telemetry.system : {}
    const soc = masterSoc()
    if (soc != null && soc <= 98) {
      state.floatVoltage = FLOAT_DEFAULT_VOLTAGE
      state.floatQualified = false
      state.floatQualifiedAt = null
      state.floatSampleCount = 0
      state.floatVoltageSum = 0
      return
    }
    if (state.floatQualified) return
    if (!finite(system.maximumCellVoltage61) || !finite(system.voltage61) || system.maximumCellVoltage61 < FLOAT_CELL_THRESHOLD) {
      state.floatSampleCount = 0
      state.floatVoltageSum = 0
      return
    }
    state.floatSampleCount += 1
    state.floatVoltageSum += system.voltage61
    if (state.floatSampleCount >= config.qualificationSamples) {
      state.floatVoltage = state.floatVoltageSum / state.floatSampleCount
      state.floatQualified = true
      state.floatQualifiedAt = timestamp
      record(timestamp, 'FLOAT_VOLTAGE_QUALIFIED', 'pack-voltage average frozen after qualified high-cell samples', {
        floatVoltage: state.floatVoltage,
        samples: state.floatSampleCount
      })
    }
  }

  function updateSampleQualifications (timestamp, health) {
    if (!health.valid) {
      resetIncompleteQualifications()
      return false
    }
    const sampleTimestamp = telemetry && telemetry.timestamp
    if (!finite(sampleTimestamp) || sampleTimestamp === state.lastQualificationTelemetryTimestamp) return false
    state.lastQualificationTelemetryTimestamp = sampleTimestamp
    updateFloatVoltageSample(timestamp)

    if (state.automaticBalancingEnabled && !state.completionLatched) {
      const next = {}
      for (const battery of batteries()) {
        next[battery.address] = eligible(battery)
          ? (state.entryQualificationSamples[battery.address] || 0) + 1
          : 0
      }
      state.entryQualificationSamples = next
    } else {
      state.entryQualificationSamples = {}
    }

    const selected = batteryByAddress(state.selectedAddress)
    state.exitQualificationSamples = state.state === STATES.BALANCING && selected &&
      selected.current > BALANCE_EXIT_CURRENT_THRESHOLD && selected.spread < config.balancerSpreadThreshold
      ? state.exitQualificationSamples + 1
      : 0
    return true
  }

  function entryQualified (battery) {
    return eligible(battery) && (state.entryQualificationSamples[battery.address] || 0) >= config.qualificationSamples
  }

  function selectCandidate (timestamp, excludedAddress = null, manualAddress = null) {
    const ordered = batteries().slice().sort((a, b) => a.address - b.address)
    const candidate = manualAddress != null
      ? ordered.find(battery => battery.address === manualAddress && battery.address !== excludedAddress && entryQualified(battery))
      : ordered.find(battery => battery.address !== excludedAddress && entryQualified(battery))
    if (!candidate) return null
    state.selectedAddress = candidate.address
    state.selectedReason = manualAddress != null ? 'manual operator selection' : 'first eligible battery in address order'
    resetControl()
    transition(STATES.BALANCING, timestamp, `battery ${candidate.address} selected`)
    record(timestamp, 'BATTERY_SELECTED', state.selectedReason, { selectedAddress: candidate.address, spread: candidate.spread })
    return candidate
  }

  function allExpectedAtFullSoc () {
    const expected = expectedAddresses()
    return expected.length > 0 && expected.every(address => {
      const battery = batteryByAddress(address)
      return battery61Valid(battery) && batterySoc(battery) === 100
    })
  }

  function updateCompletionRearm (timestamp) {
    if (!state.completionLatched) return
    if (!chargePermitted()) {
      state.permissionOffSeen = true
      state.permissionOnStartedAt = null
      return
    }
    if (!state.permissionOffSeen) return
    state.permissionOnStartedAt = state.permissionOnStartedAt || timestamp
    if (timestamp - state.permissionOnStartedAt >= config.permissionRearmDebounceMs) {
      state.completionLatched = false
      state.permissionOffSeen = false
      state.permissionOnStartedAt = null
      record(timestamp, 'BALANCE_REARMED', 'charge permission remained enabled after an observed disabled state')
    }
  }

  function positiveTotalCurrent () {
    return batteries().reduce((total, battery) => {
      if (!battery61Valid(battery) || !chargeMosfetOn(battery) || localProtection(battery)) return total
      return total + Math.max(0, battery.current)
    }, 0)
  }

  function effectiveControl () { return telemetry && telemetry.effectiveControl ? telemetry.effectiveControl : {} }
  function effectiveCcl () {
    const value = effectiveControl().effectiveChargeCurrent
    return finite(value) ? Math.max(0, value) : ccl()
  }

  function balancingCommand (timestamp, battery) {
    const sampleTimestamp = finite(telemetry && telemetry.timestamp) ? telemetry.timestamp : timestamp
    const selectedCurrent = battery.current
    const totalCurrent = positiveTotalCurrent()
    const error = config.balanceBatteryCurrentTarget - selectedCurrent
    const previousSample = state.lastControlSampleTimestamp
    if (previousSample === sampleTimestamp) return commandFromState(error, 'BALANCING_DUPLICATE_SAMPLE')

    const dt = previousSample == null ? 0 : Math.max(0, (sampleTimestamp - previousSample) / 1000)
    state.lastControlSampleTimestamp = sampleTimestamp
    const heldRequest = state.aggregateCurrentCommand
    const control = effectiveControl()
    const effective = effectiveCcl()
    const thermalFactor = finite(control.thermalFactor) ? control.thermalFactor : 1
    const permission = chargePermitted()
    const bmsLimited = finite(effective) && effective < heldRequest - 0.05
    const thermalLimited = thermalFactor < 0.999
    const canClassifySolar = permission && chargeMosfetOn(battery) && !thermalLimited && !bmsLimited && ccl() !== 0
    const solarDeficit = canClassifySolar && selectedCurrent < config.balanceBatteryCurrentTarget - config.currentTargetTolerance &&
      totalCurrent < heldRequest - config.solarShortfallTolerance

    if (!state.solarLimited) {
      state.solarDeficitSamples = solarDeficit ? state.solarDeficitSamples + 1 : 0
      if (state.solarDeficitSamples >= config.solarDetectionSamples) {
        state.solarLimited = true
        state.solarRecoverySamples = 0
        record(timestamp, 'SOLAR_LIMITED_PAUSE', 'available charging current is below the held aggregate request', {
          selectedAddress: battery.address, selectedCurrent, positiveTotalCurrent: totalCurrent, heldRequest
        })
      }
    }

    if (state.solarLimited) {
      const recovered = Math.abs(error) <= config.currentTargetTolerance ||
        totalCurrent >= heldRequest - config.solarShortfallTolerance
      state.solarRecoverySamples = recovered ? state.solarRecoverySamples + 1 : 0
      if (state.solarRecoverySamples < config.solarDetectionSamples) {
        return commandFromState(error, 'SOLAR_LIMITED_PAUSE', totalCurrent)
      }
      state.solarLimited = false
      state.solarDeficitSamples = 0
      state.solarRecoverySamples = 0
      record(timestamp, 'SOLAR_LIMITED_RESUME', 'available charging current recovered')
    }

    const controllerFrozen = !permission || ccl() === 0 || selectedCurrent <= 0 || bmsLimited || thermalLimited
    if (controllerFrozen) {
      const reason = !permission ? 'BMS_CHARGE_PERMISSION_DISABLED' : ccl() === 0 ? 'BMS_CCL_ZERO' :
        selectedCurrent <= 0 ? 'SELECTED_CURRENT_NOT_POSITIVE' : thermalLimited ? 'THERMAL_LIMITED' : 'BMS_LIMITED'
      return commandFromState(error, reason, totalCurrent)
    }

    if (selectedCurrent >= config.shareMinimumSelectedCurrent && totalCurrent >= config.shareMinimumTotalCurrent) {
      const rawShare = clamp(selectedCurrent / totalCurrent, 0.001, 1)
      state.rawSelectedShare = rawShare
      state.filteredSelectedShare = state.filteredSelectedShare == null
        ? rawShare
        : config.feedForwardAlpha * rawShare + (1 - config.feedForwardAlpha) * state.filteredSelectedShare
    }
    state.feedForwardCurrent = state.filteredSelectedShare == null
      ? config.feedForwardFallbackCurrent
      : config.balanceBatteryCurrentTarget / state.filteredSelectedShare
    state.effectiveFeedForwardCurrent = config.feedForwardGain * state.feedForwardCurrent
    state.pTerm = config.kp * error
    let nextIntegral = state.integralTerm
    if (dt > 0) nextIntegral = clamp(nextIntegral + config.ki * error * dt, -config.maxIntegralTerm, config.maxIntegralTerm)
    if (previousSample == null) {
      state.integralTerm = nextIntegral
      return commandFromState(error, 'BALANCING_CONTROL_INITIAL', totalCurrent)
    }
    const unconstrained = state.effectiveFeedForwardCurrent + state.pTerm + nextIntegral
    const desired = clamp(unconstrained,
      config.aggregateCurrentMinimum, config.aggregateCurrentMaximum)
    const rise = config.currentIncreaseRatePerMin * dt / 60
    const request = desired > heldRequest ? Math.min(desired, heldRequest + rise) : desired
    const slewLimited = request < desired
    const saturated = desired !== unconstrained || slewLimited
    if (!(saturated && error > 0)) state.integralTerm = nextIntegral
    state.aggregateCurrentCommand = request
    return commandFromState(error, 'BALANCING_CURRENT_CONTROL', totalCurrent, saturated, slewLimited)
  }

  function commandFromState (error, reason, totalCurrent = positiveTotalCurrent(), saturated = false, slewLimited = false) {
    const settings = chargeControlSettings()
    const requestedVoltage = settings.voltageLimitEnabled === true && finite(settings.maxChargeVoltage)
      ? Math.min(config.balancingVoltageCeiling, settings.maxChargeVoltage)
      : config.balancingVoltageCeiling
    return {
      requestedVoltage,
      requestedCurrent: state.aggregateCurrentCommand,
      chargeEnabled: true,
      reason,
      pTerm: state.pTerm,
      iTerm: state.integralTerm,
      currentError: error,
      feedForwardCurrent: state.feedForwardCurrent,
      feedForwardGain: config.feedForwardGain,
      effectiveFeedForwardCurrent: state.effectiveFeedForwardCurrent,
      rawSelectedShare: state.rawSelectedShare,
      filteredSelectedShare: state.filteredSelectedShare,
      positiveTotalCurrent: totalCurrent,
      outputSaturated: saturated,
      outputSlewLimited: slewLimited,
      solarLimited: state.solarLimited
    }
  }

  function normalCommand (reason) {
    return { requestedVoltage: normalChargeVoltage(), requestedCurrent: normalChargeCurrent(), chargeEnabled: true, reason }
  }

  function safetyCommand (reason) {
    return { requestedVoltage: config.safetyFallbackVoltage, requestedCurrent: config.safetyFallbackCurrent, chargeEnabled: true, reason }
  }

  function enterSafety (timestamp, reason) {
    state.fault = reason
    resetControl()
    releaseSelection()
    transition(STATES.SAFETY_STOP, timestamp, reason)
  }

  function evaluate (timestamp) {
    const actions = []
    const health = freshTelemetry(timestamp)
    updateSampleQualifications(timestamp, health)
    let command = normalCommand('NORMAL_POLICY')

    if (!state.automaticBalancingEnabled) {
      state.fault = null
      resetControl()
      releaseSelection()
      transition(STATES.NORMAL, timestamp, 'automatic balancing disabled')
      command = normalCommand('AUTOMATIC_BALANCING_OFF')
    } else if (!health.valid) {
      enterSafety(timestamp, health.lockout)
      command = safetyCommand('SAFETY_STOP_TELEMETRY')
    } else {
      if (state.state === STATES.SAFETY_STOP) {
        state.fault = null
        transition(STATES.NORMAL, timestamp, 'telemetry and output control recovered')
      }
      updateCompletionRearm(timestamp)
      let selected = batteryByAddress(state.selectedAddress)

      if (state.state === STATES.BALANCING && allExpectedAtFullSoc()) {
        state.completionLatched = true
        state.permissionOffSeen = false
        state.permissionOnStartedAt = null
        state.lastStopReason = 'FULL_SOC_COMPLETE'
        record(timestamp, 'FULL_SOC_COMPLETE', 'all expected batteries report integer SOC 100')
        resetControl()
        releaseSelection()
        transition(STATES.NORMAL, timestamp, 'all expected batteries reached full SOC')
        command = normalCommand('FULL_SOC_COMPLETE')
      } else if (state.state === STATES.BALANCING && (!selected || !chargeMosfetOn(selected) || localProtection(selected))) {
        const releasedAddress = state.selectedAddress
        record(timestamp, 'BATTERY_CHARGE_PATH_INTERRUPTED', 'selected battery is disconnected or protected', { selectedAddress: releasedAddress })
        resetControl()
        releaseSelection()
        selected = selectCandidate(timestamp, releasedAddress)
        if (!selected) transition(STATES.NORMAL, timestamp, 'no other eligible battery')
        command = selected ? balancingCommand(timestamp, selected) : normalCommand('NO_ELIGIBLE_BATTERY')
      } else if (state.state === STATES.BALANCING && selected &&
        state.exitQualificationSamples >= config.qualificationSamples) {
        state.lastStopReason = 'BALANCE_DISCHARGE_COMPLETE'
        record(timestamp, 'BALANCE_DISCHARGE_COMPLETE', 'selected battery charged above 1.5 A with spread below threshold for the required samples', { selectedAddress: selected.address })
        resetControl()
        releaseSelection()
        transition(STATES.NORMAL, timestamp, 'selected battery discharge completion')
        command = normalCommand('BALANCE_DISCHARGE_COMPLETE')
      } else if (state.state === STATES.BALANCING && selected) {
        command = balancingCommand(timestamp, selected)
      } else if (state.state === STATES.NORMAL && !state.completionLatched) {
        selected = selectCandidate(timestamp)
        command = selected ? balancingCommand(timestamp, selected) : normalCommand('NO_ELIGIBLE_BATTERY')
      }
    }

    const controllerCommand = { version: timestamp, timestamp, ...command }
    const selected = batteryByAddress(state.selectedAddress)
    history.push({
      timestamp,
      state: state.state,
      selectedAddress: state.selectedAddress,
      selectedCurrent: selected && finite(selected.current) ? selected.current : null,
      selectedSoc: selected && Number.isInteger(selected.soc) ? selected.soc : null,
      selectedVmax: selected && finite(selected.vmax) ? selected.vmax : null,
      selectedVmin: selected && finite(selected.vmin) ? selected.vmin : null,
      selectedSpread: selected && finite(selected.spread) ? selected.spread : null,
      positiveTotalCurrent: positiveTotalCurrent(),
      aggregateCurrentCommand: state.aggregateCurrentCommand,
      effectiveCcl: effectiveCcl(),
      solarLimited: state.solarLimited
    })
    if (history.length > 360) history.splice(0, history.length - 360)

    const persistedState = { ...state }
    const persistence = { state: persistedState, config: { ...config } }
    const persistenceKey = JSON.stringify(persistence)
    actions.push({ type: 'controller_command', value: controllerCommand })
    if (pendingCsvLogging) {
      actions.push({ type: 'csv_logging', value: pendingCsvLogging })
      pendingCsvLogging = null
    }
    if (persistenceKey !== lastPersistenceKey) {
      lastPersistenceKey = persistenceKey
      actions.push({ type: 'persist', ...persistence })
    }
    actions.push({ type: 'status', status: status(timestamp, health, controllerCommand) })
    return actions
  }

  function status (timestamp, health = freshTelemetry(timestamp), command = null) {
    const selected = batteryByAddress(state.selectedAddress)
    const excludedBatteries = batteries().filter(battery => !eligible(battery)).map(battery => ({
      address: battery.address,
      mosfetOn: chargeMosfetOn(battery),
      protected: localProtection(battery),
      system61Valid: battery.system61Valid === true,
      spread: finite(battery.spread) ? battery.spread : null
    }))
    const authorityState = telemetry && telemetry.activeBms
      ? (telemetry.activeBms.readbackValid === true
          ? (telemetry.activeBms.virtualSelected === true ? 'APPLIED' : 'SHADOW')
          : 'UNKNOWN')
      : 'UNKNOWN'
    const currentPolicy = normalCurrentPolicy()
    return {
      state: state.state,
      automaticBalancingEnabled: state.automaticBalancingEnabled,
      selectedAddress: state.selectedAddress,
      selectedReason: state.selectedReason,
      lastStopReason: state.lastStopReason,
      fault: state.fault,
      lockout: health.lockout,
      authorityState,
      controllerRequestApplied: Boolean(telemetry && telemetry.effectiveControl && telemetry.effectiveControl.controllerRequestApplied),
      telemetry: telemetry ? { ...telemetry, telemetryAge: health.age, valid: health.valid } : null,
      activeBms: telemetry && telemetry.activeBms ? { ...telemetry.activeBms } : null,
      virtualBmsSelected: virtualBmsSelected(),
      virtualBms: telemetry && telemetry.effectiveControl ? { ...telemetry.effectiveControl } : null,
      chargeControlSettings: { ...chargeControlSettings() },
      csvLogging: { ...state.csvLogging },
      selectedCurrent: selected && selected.current,
      selectedSoc: selected ? batterySoc(selected) : masterSoc(),
      selectedVmin: selected && selected.vmin,
      selectedVmax: selected && selected.vmax,
      selectedSpread: selected && selected.spread,
      selectedEffectiveDischarging: selected ? effectiveDischarging(selected) : false,
      positiveTotalCurrent: positiveTotalCurrent(),
      rawSelectedShare: state.rawSelectedShare,
      filteredSelectedShare: state.filteredSelectedShare,
      feedForwardCurrent: state.feedForwardCurrent,
      effectiveFeedForwardCurrent: state.effectiveFeedForwardCurrent,
      pTerm: state.pTerm,
      iTerm: state.integralTerm,
      aggregateCurrentCommand: state.aggregateCurrentCommand,
      solarLimited: state.solarLimited,
      solarDeficitSamples: state.solarDeficitSamples,
      solarRecoverySamples: state.solarRecoverySamples,
      completionLatched: state.completionLatched,
      normalCurrentPolicy: currentPolicy,
      entryQualification: batteries().map(battery => ({
        address: battery.address,
        samples: state.entryQualificationSamples[battery.address] || 0,
        required: config.qualificationSamples,
        qualified: entryQualified(battery)
      })),
      exitQualification: {
        samples: state.exitQualificationSamples,
        required: config.qualificationSamples,
        currentThreshold: BALANCE_EXIT_CURRENT_THRESHOLD,
        qualified: state.exitQualificationSamples >= config.qualificationSamples
      },
      floatControl: {
        voltage: state.floatVoltage,
        qualified: state.floatQualified,
        qualifiedAt: state.floatQualifiedAt,
        samples: state.floatQualified ? config.qualificationSamples : state.floatSampleCount,
        required: config.qualificationSamples,
        maximumCellThreshold: FLOAT_CELL_THRESHOLD,
        active: floatCvlActive()
      },
      permissionOffSeen: state.permissionOffSeen,
      permissionOnStartedAt: state.permissionOnStartedAt,
      excludedBatteries,
      lastCommand: command,
      config: { ...config },
      configurationResult,
      history: history.slice(),
      availableEvents: events.slice(-100),
      lastEvent: state.lastEvent
    }
  }

  function handle (message = {}) {
    const timestamp = finite(message.timestamp) ? message.timestamp : now()
    if (message.type === 'load') {
      const persistedVersion = message.state && message.state.version
      const currentVersion = [3, 4, 5, CONTROLLER_VERSION].includes(persistedVersion)
      state = migrateState(message.state || {})
      const candidateConfig = currentVersion ? { ...(message.config || {}) } : {}
      if (persistedVersion !== CONTROLLER_VERSION && candidateConfig.ki === 0.002) candidateConfig.ki = DEFAULT_CONFIG.ki
      const validated = validateConfig(candidateConfig)
      config = validated.config
      resetControl()
      if (!validated.valid) {
        state.fault = validated.errors.join('; ')
        state.state = STATES.SAFETY_STOP
      }
      return evaluate(timestamp)
    }
    if (message.type === 'telemetry') {
      telemetry = { ...message.telemetry }
      return evaluate(timestamp)
    }
    if (message.type === 'tick') return evaluate(timestamp)
    if (message.type === 'set_automatic_balancing') {
      state.automaticBalancingEnabled = message.value === true
      if (!state.automaticBalancingEnabled) {
        state.fault = null
        resetControl()
        releaseSelection()
        transition(STATES.NORMAL, timestamp, 'automatic balancing disabled')
      }
      return evaluate(timestamp)
    }
    if (message.type === 'set_csv_logging') {
      const filename = typeof message.filename === 'string' ? message.filename.trim() : null
      if (message.value === true && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,80}(?:\.csv)?$/.test(filename || '')) {
        record(timestamp, 'csv_logging_rejected', 'invalid CSV filename')
        return evaluate(timestamp)
      }
      const normalized = message.value === true ? (filename.endsWith('.csv') ? filename : `${filename}.csv`) : null
      state.csvLogging = { enabled: message.value === true, filename: normalized }
      pendingCsvLogging = { ...state.csvLogging, timestamp }
      return evaluate(timestamp)
    }
    if (message.type === 'reset_integrator') {
      resetControl()
      record(timestamp, 'integrator_reset', 'operator reset')
      return evaluate(timestamp)
    }
    if (message.type === 'restore_defaults') {
      config = { ...DEFAULT_CONFIG }
      state.automaticBalancingEnabled = true
      state.completionLatched = false
      state.permissionOffSeen = false
      state.permissionOnStartedAt = null
      state.lastStopReason = null
      state.fault = null
      resetIncompleteQualifications()
      state.floatVoltage = FLOAT_DEFAULT_VOLTAGE
      state.floatQualified = false
      state.floatQualifiedAt = null
      resetControl()
      releaseSelection()
      transition(STATES.NORMAL, timestamp, 'controller defaults restored')
      record(timestamp, 'defaults_restored', 'automatic balancing enabled and controller defaults restored')
      return evaluate(timestamp)
    }
    if (message.type === 'configure') {
      const validated = validateConfig(message.config)
      const requestId = message.requestId == null ? null : String(message.requestId)
      configurationResult = { requestId, accepted: validated.valid, errors: validated.errors.slice() }
      if (validated.valid) config = validated.config
      else record(timestamp, 'config_rejected', validated.errors.join('; '))
      return evaluate(timestamp)
    }
    return evaluate(timestamp)
  }

  return {
    handle,
    getState: () => ({ ...state }),
    getConfig: () => ({ ...config }),
    getStatus: timestamp => status(timestamp || now()),
    getEvents: () => events.slice()
  }
}

module.exports = { STATES, CONTROLLER_VERSION, DEFAULT_CONFIG, validateConfig, createBalancerController }
