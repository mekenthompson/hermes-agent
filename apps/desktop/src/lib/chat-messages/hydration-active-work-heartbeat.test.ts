import { describe, expect, it } from 'vitest'

import { toChatMessages } from './hydration'

const PROMPT = '[Heartbeat — recurring instruction, fires every 3m]\nCheck active tracked work that originated in this chat'
const managed = { active_work_heartbeat: true }

const turn = (reply: string, metadata: unknown = managed) => [
  { role: 'user', content: 'start the build', timestamp: 100 },
  { role: 'assistant', content: 'building', timestamp: 101 },
  { role: 'user', content: PROMPT, display_kind: 'internal_notification', display_metadata: metadata, timestamp: 200 },
  { role: 'assistant', content: reply, timestamp: 201 },
  { role: 'user', content: 'thanks', timestamp: 300 }
]

const texts = (messages: unknown[]) =>
  toChatMessages(messages as never).map(m => `${m.role}:${m.parts.map(p => ('text' in p ? p.text : '')).join('')}`)

// A managed active-work check that found nothing new answers with the exact silence marker. Desktop used to paint
// the machinery prompt and that marker as two chat bubbles.
describe('toChatMessages with a managed active-work heartbeat turn', () => {
  it.each(['[SILENT]', 'SILENT', ' [silent] ', 'NO_REPLY', '*[SILENT]*', '[静默]'])('collapses the turn answered with %j', reply => {
    expect(texts(turn(reply))).toEqual(['user:start the build', 'assistant:building', 'user:thanks'])
  })

  it('reads the mark from an older backend that serves display_metadata as JSON text', () => {
    expect(texts(turn('[SILENT]', JSON.stringify(managed)))).toEqual([
      'user:start the build',
      'assistant:building',
      'user:thanks'
    ])
  })

  it('keeps a turn whose reply is a real update, and prose that merely mentions the marker', () => {
    for (const reply of ['Build is at 80%, canary is next.', 'Silent builds finished.', 'It replied [SILENT] earlier']) {
      expect(texts(turn(reply))).toContain(`assistant:${reply}`)
    }
  })

  it('keeps the turn until the model has replied', () => {
    expect(texts(turn('[SILENT]').slice(0, 3))).toContain(`user:${PROMPT}`)
  })

  it('never collapses a user-authored heartbeat, marked or not by its silence', () => {
    expect(texts(turn('[SILENT]', null))).toContain('assistant:[SILENT]')
    expect(texts(turn('[SILENT]', { active_work_heartbeat: false }))).toContain('assistant:[SILENT]')
  })

  it('keeps later rows keyed by their stored position', () => {
    const result = toChatMessages(turn('[SILENT]') as never)

    expect(result.map(m => m.id)).toEqual(['100-0-user', '101-1-assistant', '300-4-user'])
  })
})
