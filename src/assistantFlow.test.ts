import { describe, expect, it } from 'vitest'

import {
  groupAssistantFlowEntries,
  type AssistantFlowEntry,
} from './assistantFlow'

const delegationRow = (taskId: string): AssistantFlowEntry => ({
  id: `entry_delegation_${taskId}`,
  label: `Delegated x_search to X search`,
  status: 'running',
  kind: 'delegation',
  createdAt: '2026-09-14T00:00:00Z',
  flowRole: 'delegation',
  delegatedTaskId: taskId,
})

const specialistRow = (
  parentTaskId: string,
  overrides: Partial<AssistantFlowEntry> = {},
): AssistantFlowEntry => ({
  id: `entry_specialist_${parentTaskId}_${overrides.label ?? 'step'}`,
  label: 'X twitter search agent: Searching X for shipping chatter',
  status: 'running',
  kind: 'specialist_flow',
  createdAt: '2026-09-14T00:01:00Z',
  flowRole: 'specialist',
  parentDelegatedTaskId: parentTaskId,
  specialistTaskId: `spec_${parentTaskId}`,
  agentId: 'cosmic/x-twitter-search-agent',
  agentLabel: 'X search',
  intent: 'x_search',
  ...overrides,
})

const genericRow = (label: string): AssistantFlowEntry => ({
  id: `entry_generic_${label.replace(/\s+/g, '_')}`,
  label,
  status: 'running',
  kind: 'generic',
  createdAt: '2026-09-14T00:02:00Z',
})

describe('groupAssistantFlowEntries', () => {
  it('keeps the historical grouping when real delegation rows exist', () => {
    const delegation = delegationRow('t1')
    const first = specialistRow('t1', { label: 'step one' })
    const second = specialistRow('t1', { label: 'step two' })
    const generic = genericRow('Drafting the reply')

    const { roots, childrenByParent } = groupAssistantFlowEntries([
      delegation,
      first,
      second,
      generic,
    ])

    expect(roots).toEqual([delegation, generic])
    expect(childrenByParent.get('t1')).toEqual([first, second])
    expect(roots.some((entry) => entry.id === 'delegated_t1')).toBe(false)
  })

  it('re-branches a replayed log that lost its delegation rows', () => {
    const first = specialistRow('t1', { label: 'step one' })
    const second = specialistRow('t1', { label: 'step two' })
    const generic = genericRow('Drafting the reply')

    const { roots, childrenByParent } = groupAssistantFlowEntries([
      generic,
      first,
      second,
    ])

    expect(roots).toHaveLength(2)
    expect(roots[0]).toBe(generic)
    const synthesized = roots[1]
    expect(synthesized.id).toBe('delegated_t1')
    expect(synthesized.label).toBe('Delegated x_search to X search')
    expect(synthesized.flowRole).toBe('delegation')
    expect(synthesized.delegatedTaskId).toBe('t1')
    expect(synthesized.parentDelegatedTaskId).toBeNull()
    expect(childrenByParent.get('t1')).toEqual([first, second])
  })

  it('synthesizes one root per missing parent, in first-child order', () => {
    const a1 = specialistRow('t1', { label: 'a one', id: 'a1' })
    const b1 = specialistRow('t2', { label: 'b one', id: 'b1' })
    const a2 = specialistRow('t1', { label: 'a two', id: 'a2' })

    const { roots, childrenByParent } = groupAssistantFlowEntries([a1, b1, a2])

    expect(roots.map((entry) => entry.id)).toEqual(['delegated_t1', 'delegated_t2'])
    expect(childrenByParent.get('t1')).toEqual([a1, a2])
    expect(childrenByParent.get('t2')).toEqual([b1])
  })

  it('prefers the real delegation row when it arrives after the children', () => {
    const delegation = delegationRow('t1')
    const first = specialistRow('t1', { label: 'step one' })

    const { roots, childrenByParent } = groupAssistantFlowEntries([first, delegation])

    expect(roots).toEqual([delegation])
    expect(childrenByParent.get('t1')).toEqual([first])
    expect(roots.some((entry) => entry.id === 'delegated_t1')).toBe(false)
  })

  it('leaves entries without parent references untouched', () => {
    const generic = genericRow('Reading the trace')
    const { roots, childrenByParent } = groupAssistantFlowEntries([generic])

    expect(roots).toEqual([generic])
    expect(childrenByParent.size).toBe(0)
  })

  it('falls back to a generic label when the child carries no intent', () => {
    const child = specialistRow('t1', { intent: null, agentLabel: null })

    const { roots } = groupAssistantFlowEntries([child])

    expect(roots).toHaveLength(1)
    expect(roots[0].label).toBe('Delegated specialist work')
  })

  it('does not leak synthesis state between calls', () => {
    const child = specialistRow('t1')
    groupAssistantFlowEntries([child])
    const second = groupAssistantFlowEntries([child])

    expect(second.roots).toHaveLength(1)
    expect(second.childrenByParent.get('t1')).toEqual([child])
  })
})
