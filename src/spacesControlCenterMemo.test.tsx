import { describe, expect, it } from 'vitest'
import SpacesControlCenter from './SpacesControlCenter'

/* The spaces surface is permanently mounted inside the overlay and one render
 * pass over it costs 100ms+. It is exported through React.memo so that App's
 * high-frequency state changes (gateway events, stream chunks, hover tooltips)
 * skip it entirely — without that boundary, every event re-rendered the whole
 * control center and the spaces screen felt hung while an agent was active.
 * Dropping the memo silently reintroduces that hang, so pin it here. */
describe('SpacesControlCenter export', () => {
  it('is wrapped in React.memo so app-wide re-render storms skip it', () => {
    expect(typeof SpacesControlCenter).toBe('object')
    expect((SpacesControlCenter as { $$typeof?: symbol }).$$typeof).toBe(Symbol.for('react.memo'))
  })
})
