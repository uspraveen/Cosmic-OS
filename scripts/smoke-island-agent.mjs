#!/usr/bin/env node
/**
 * Cosmic-OS — Dynamic Island agent-at-work smoke script.
 *
 * Triggers the Dynamic Island's "agent at work" slide so we can iterate on
 * the visuals (spinning globe, shimmer text, etc.) without running a real
 * orchestrator turn.
 *
 * Usage:
 *   node scripts/smoke-island-agent.mjs --agent web-search
 *   node scripts/smoke-island-agent.mjs --agent web-search --label "Searching the web" --detail "openai gpt-5 release notes"
 *
 * Modes:
 *   (default)  Spawns `npm run dev` with VITE_SMOKE_ISLAND_AGENT env vars set.
 *              The renderer reads these on mount and auto-fires the slide.
 *
 *   --print    Skip spawning. Print a one-line `window.dispatchEvent(...)`
 *              snippet you can paste into the running app's DevTools console.
 *
 * Flags:
 *   --agent    <id>     Required. e.g. "web-search", "firecrawl", "perplexity".
 *   --label    <text>   Optional. Headline shown next to the visual.
 *   --detail   <text>   Optional. Smaller line under the headline (e.g. query).
 *   --print             Don't spawn — just print the DevTools snippet.
 *   --help              Show this help.
 *
 * Examples:
 *   # Cold start: launches a fresh dev server with the slide visible.
 *   node scripts/smoke-island-agent.mjs --agent web-search --detail "anthropic claude 4.7"
 *
 *   # Already have `npm run dev` running? Get a snippet to paste:
 *   node scripts/smoke-island-agent.mjs --agent web-search --print
 */

import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = path.resolve(HERE, '..')

function parseArgs(argv) {
  const out = { _print: false, _help: false }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--help' || a === '-h') { out._help = true; continue }
    if (a === '--print') { out._print = true; continue }
    if (a.startsWith('--')) {
      const key = a.slice(2)
      const val = argv[i + 1]
      if (val === undefined || val.startsWith('--')) {
        out[key] = true
      } else {
        out[key] = val
        i++
      }
      continue
    }
  }
  return out
}

function printHelp() {
  // Re-print the header comment block (without the leading * markers).
  const lines = [
    '',
    'cosmic smoke-island-agent — drive the Dynamic Island agent-at-work slide',
    '',
    'Usage:',
    '  node scripts/smoke-island-agent.mjs --agent <id> [--label "..."] [--detail "..."]',
    '  node scripts/smoke-island-agent.mjs --agent <id> --print',
    '',
    'Flags:',
    '  --agent    <id>    Required. e.g. web-search | firecrawl | perplexity',
    '  --label    <text>  Optional headline (defaults to preset label)',
    '  --detail   <text>  Optional sub-line (e.g. the search query)',
    '  --print            Print a DevTools-paste snippet instead of spawning dev',
    '  --help             Show this help',
    '',
  ]
  console.log(lines.join('\n'))
}

const args = parseArgs(process.argv.slice(2))
if (args._help) {
  printHelp()
  process.exit(0)
}

const agentId = typeof args.agent === 'string' ? args.agent : null
if (!agentId) {
  console.error('error: --agent <id> is required (e.g. --agent web-search)')
  console.error('       run with --help for usage.')
  process.exit(2)
}

const label = typeof args.label === 'string' ? args.label : undefined
const detail = typeof args.detail === 'string' ? args.detail : undefined

const payload = { agentId, ...(label ? { label } : {}), ...(detail ? { detail } : {}) }
const snippet =
  `window.dispatchEvent(new CustomEvent('cosmic:island-agent-work', { detail: ${JSON.stringify(payload)} }))`
const stopSnippet =
  `window.dispatchEvent(new CustomEvent('cosmic:island-agent-work', { detail: { stop: true } }))`

if (args._print) {
  console.log('— Open the Cosmic-OS DevTools (Ctrl+Shift+I in the app window) —')
  console.log('   and paste ONE of the following into the Console tab:')
  console.log('')
  console.log('  ', snippet)
  console.log('')
  console.log('   or, equivalently:')
  console.log('')
  console.log('  ', `__cosmicSmokeIsland(${JSON.stringify(payload)})`)
  console.log('')
  console.log('— To clear the slide —')
  console.log('')
  console.log('  ', stopSnippet)
  console.log('')
  console.log('   or:')
  console.log('')
  console.log('  ', `__cosmicSmokeIsland(null)`)
  console.log('')
  process.exit(0)
}

const isWindows = process.platform === 'win32'

const env = {
  ...process.env,
  VITE_SMOKE_ISLAND_AGENT: agentId,
  ...(label ? { VITE_SMOKE_ISLAND_LABEL: label } : {}),
  ...(detail ? { VITE_SMOKE_ISLAND_DETAIL: detail } : {}),
}

console.log('▸ launching Cosmic-OS dev with smoke island agent:')
console.log('   agent  =', agentId)
if (label)  console.log('   label  =', label)
if (detail) console.log('   detail =', detail)
console.log('')
console.log('   (the slide auto-fires once the island mounts; ctrl+c to stop)')
console.log('')

// Node 20+ on Windows refuses to spawn .cmd shims unless `shell: true` is set
// (CVE-2024-27980). `shell: true` runs the command via cmd.exe / sh, so we
// must shell-quote any user-provided arguments. We don't pass any here, but
// keep the surface minimal: the only token is `npm run dev`.
const child = spawn('npm', ['run', 'dev'], {
  cwd: PROJECT_ROOT,
  env,
  stdio: 'inherit',
  shell: isWindows ? true : false,
})

const forward = (sig) => () => {
  if (!child.killed) {
    try { child.kill(sig) } catch { /* ignore */ }
  }
}
process.on('SIGINT', forward('SIGINT'))
process.on('SIGTERM', forward('SIGTERM'))

child.on('exit', (code) => process.exit(code ?? 0))
