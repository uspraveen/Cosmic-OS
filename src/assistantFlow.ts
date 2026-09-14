/**
 * Grouping for the assistant "Flow" timeline — orchestrator steps with the
 * specialist (subagent) work nested under their invocation rows.
 *
 * Live, the client accumulates two entry kinds from task.progress events:
 * delegation rows (flowRole 'delegation', carrying delegatedTaskId) and
 * specialist rows (flowRole 'specialist', carrying parentDelegatedTaskId).
 * The persisted task notebook only records the specialist rows — the gateway
 * never writes delegation rows — so a replayed log (or the server's
 * authoritative copy that lands on task lifecycle events) arrives without
 * the parents and the branch would flatten into a linear list.
 *
 * groupAssistantFlowEntries nests children under their real delegation row
 * when one exists, and otherwise synthesizes the missing invocation root
 * from the children's own metadata (stable `delegated_<taskId>` id, placed
 * where the branch starts). With real delegation rows present the grouping
 * is exactly the historical behavior — synthesis never triggers.
 */

export interface AssistantFlowEntry {
  id: string
  label: string
  detail?: string
  status?: string | null
  stage?: string | null
  kind?: string | null
  createdAt: string
  flowRole?: string | null
  delegatedTaskId?: string | null
  parentDelegatedTaskId?: string | null
  specialistTaskId?: string | null
  agentId?: string | null
  agentLabel?: string | null
  intent?: string | null
  specialistEventType?: string | null
  previewUrl?: string | null
  slideNumber?: number | null
}

export interface GroupedAssistantFlowEntries {
  roots: AssistantFlowEntry[]
  childrenByParent: Map<string, AssistantFlowEntry[]>
}

const taskRef = (value: unknown): string => String(value || '').trim()

const buildSynthesizedDelegation = (
  parentTaskId: string,
  firstChild: AssistantFlowEntry,
): AssistantFlowEntry => {
  const intent = taskRef(firstChild.intent) || 'specialist work'
  const agentLabel = taskRef(firstChild.agentLabel)
  return {
    ...firstChild,
    id: `delegated_${parentTaskId}`,
    label: agentLabel ? `Delegated ${intent} to ${agentLabel}` : `Delegated ${intent}`,
    detail: undefined,
    status: null,
    kind: 'delegation',
    flowRole: 'delegation',
    delegatedTaskId: parentTaskId,
    parentDelegatedTaskId: null,
    specialistTaskId: null,
    specialistEventType: null,
    previewUrl: null,
    slideNumber: null,
  }
}

export const groupAssistantFlowEntries = (
  entries: AssistantFlowEntry[],
): GroupedAssistantFlowEntries => {
  const delegationByTaskId = new Map<string, AssistantFlowEntry>()
  for (const entry of entries) {
    const delegatedTaskId = taskRef(entry.delegatedTaskId)
    if (delegatedTaskId) {
      delegationByTaskId.set(delegatedTaskId, entry)
    }
  }

  const childrenByParent = new Map<string, AssistantFlowEntry[]>()
  const synthesizedByParent = new Map<string, AssistantFlowEntry>()
  const roots: AssistantFlowEntry[] = []
  for (const entry of entries) {
    const parentTaskId = taskRef(entry.parentDelegatedTaskId)
    if (parentTaskId && delegationByTaskId.has(parentTaskId)) {
      childrenByParent.set(parentTaskId, [...(childrenByParent.get(parentTaskId) || []), entry])
      continue
    }
    if (parentTaskId) {
      // The persisted log dropped this delegation row; re-create it in place
      // so the branch keeps its shape instead of flattening.
      let synthesized = synthesizedByParent.get(parentTaskId)
      if (!synthesized) {
        synthesized = buildSynthesizedDelegation(parentTaskId, entry)
        synthesizedByParent.set(parentTaskId, synthesized)
        roots.push(synthesized)
      }
      childrenByParent.set(parentTaskId, [...(childrenByParent.get(parentTaskId) || []), entry])
      continue
    }
    roots.push(entry)
  }
  return { roots, childrenByParent }
}
