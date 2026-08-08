'use strict'

const fs = require('fs')

if (process.argv.length !== 4) {
  console.error('usage: node scripts/merge-flows.js existing.json addition.json')
  process.exit(2)
}

const existingPath = process.argv[2]
const additionPath = process.argv[3]
const existing = JSON.parse(fs.readFileSync(existingPath, 'utf8'))
const addition = JSON.parse(fs.readFileSync(additionPath, 'utf8'))
const sharedConfiguration = new Set(['ui-base', 'ui-theme', 'victron-client'])
const obsoleteNodeIds = new Set(['bal-rs485-service', 'bal-rs485-start'])

// A previous merge can leave retired balancer nodes behind because Node-RED's
// flow export is an additive document.  Remove only the explicitly retired
// poller nodes and collapse duplicate IDs before applying the current flow.
const merged = []
const seenIds = new Set()
for (const node of existing) {
  if (obsoleteNodeIds.has(node.id) || seenIds.has(node.id)) continue
  seenIds.add(node.id)
  merged.push(node)
}

const byId = new Map(merged.map(node => [node.id, node]))

for (const node of addition) {
  if (sharedConfiguration.has(node.type) && byId.has(node.id)) continue
  const previous = byId.get(node.id)
  if (previous) merged[merged.indexOf(previous)] = node
  else merged.push(node)
  byId.set(node.id, node)
}

process.stdout.write(`${JSON.stringify(merged, null, 4)}\n`)
