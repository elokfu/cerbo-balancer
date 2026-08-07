'use strict'

const assert = require('assert/strict')
const fs = require('fs')
const path = require('path')

const flowPath = path.join(__dirname, '..', 'flow', 'cerbo-balancer-controller.json')
const flow = JSON.parse(fs.readFileSync(flowPath, 'utf8'))
const text = fs.readFileSync(flowPath, 'utf8')
assert.ok(flow.some(node => node.id === 'bal-can-reader'))
assert.ok(flow.some(node => node.type === 'victron-input-battery'))
assert.ok(flow.some(node => node.type === 'victron-input-vebus'))
assert.ok(flow.some(node => node.type === 'ui-page' && node.path === '/balancer'))
assert.ok(text.includes('TEST_MODE'))
assert.ok(text.includes('can1'))
assert.ok(text.includes('cerbo-balancer-state.json'))
assert.ok(!text.includes('victron-output-settings'))
assert.ok(!text.includes('cansend'))
assert.ok(!text.includes('cangen'))
