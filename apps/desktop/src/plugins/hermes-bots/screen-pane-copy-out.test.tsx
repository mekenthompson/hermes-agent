/**
 * "Copy from screen": only the viewer holding the lease sees the action; it asks the gateway
 * (`display.clipboard.read`, with THIS window's viewer id) and writes the text to the user's own clipboard.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { DisplayStatus } from './screen-connection'
import type * as ScreenConnection from './screen-connection'
import type { RosterRow } from './types'

vi.mock('@hermes/plugin-sdk', async () => {
  const { useStore } = await import('@nanostores/react')
  const { onGatewayEvent } = await import('../../contrib/events')

  return {
    Button: ({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) => (
      <button {...props}>{children}</button>
    ),
    Codicon: () => null,
    GlyphSpinner: () => null,
    Tip: ({ children }: { children: ReactNode }) => <>{children}</>,
    EmptyState: () => null,
    useValue: useStore,
    host: { onEvent: onGatewayEvent, retainProfile: async () => () => {} }
  }
})
vi.mock('./routing', () => {
  const route = { connectionId: 'host-a', mode: 'remote', profile: 'default', targetProfile: 'default' }

  return { botConnectionRoute: () => route, resolveBotConnectionRoute: () => ({ status: 'resolved', route }) }
})
vi.mock('./data', () => ({ botSelectionKey: (bot: RosterRow) => bot.name }))
vi.mock('./i18n', () => ({
  useBots: () => ({
    screen: {
      title: 'Screen',
      youControl: 'You control',
      handBack: 'Hand back',
      takeOver: 'Take over',
      reconnect: 'Reconnect',
      streamLost: 'Stream lost',
      copyFromScreen: 'Copy from screen',
      copyFromScreenHint: 'hint',
      copiedFromScreen: 'Copied',
      copyEmpty: 'Nothing copied',
      copyFailed: 'Copy failed'
    }
  })
}))
vi.mock('./screen-connection', async importActual => ({
  ...(await importActual<typeof ScreenConnection>()),
  displayRequest: vi.fn(),
  resolveScreenWsUrl: vi.fn(async () => 'ws://localhost/api/display/ws'),
  isEventForBotScreen: () => false
}))
vi.mock('@novnc/novnc', () => ({
  default: class {
    viewOnly = true
    clipboardPasteFrom = vi.fn()
    addEventListener(type: string, callback: (event: { detail?: unknown }) => void) {
      if (type === 'connect') {
        queueMicrotask(() => callback({}))
      }
    }
    disconnect() {}
    focus() {}
  }
}))

import { displayRequest } from './screen-connection'
import { BotScreenPane } from './screen-pane'
import { $screenState } from './screen-state'

const bot: RosterRow = { name: 'default' }

// `viewer_hash` is the first 12 hex of sha256("this-viewer"): the lease names THIS window as its holder.
const status: DisplayStatus = {
  profile: 'default',
  profile_key: '/home/hermes/.hermes',
  supported: true,
  installed: true,
  missing: [],
  running: true,
  pid: 42,
  display: ':20',
  socket: '/tmp/rfb.sock',
  geometry: '1440x900',
  install_command: null,
  lease: { holder: 'human', viewer_id: null, viewer_hash: 'e0f9a555d558', since: 1, reason: '', epoch: 1 }
}

const writeText = vi.fn(async (_text: string) => {})
let clipboardText: () => Promise<{ text: string }>

function serve(lease = status.lease) {
  vi.mocked(displayRequest)
    .mockReset()
    .mockImplementation(async (_bot, method) =>
      method === 'display.clipboard.read'
        ? clipboardText()
        : ({ ...status, lease, ticket: 'test-ticket', viewer_id: 'this-viewer' } as never)
    )
}

beforeEach(() => {
  $screenState.set({})
  writeText.mockClear()
  clipboardText = async () => ({ text: 'copied on the screen' })
  serve()
  vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
  vi.stubGlobal(
    'WebSocket',
    class {
      binaryType = ''
      addEventListener() {}
      close() {}
    }
  )
})

afterEach(() => vi.unstubAllGlobals())

it("copies the screen text to the local clipboard, asking as this window's viewer", async () => {
  const view = render(<BotScreenPane bot={bot} />)
  const button = await screen.findByRole('button', { name: /Copy from screen/ })
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))

  fireEvent.click(button)

  await waitFor(() => expect(writeText).toHaveBeenCalledWith('copied on the screen'))
  expect(displayRequest).toHaveBeenCalledWith(bot, 'display.clipboard.read', { viewer_id: 'this-viewer' })
  expect(await screen.findByText('Copied')).toBeTruthy()
  view.unmount()
})

it('offers no copy action to a viewer that is only watching', async () => {
  serve({ ...status.lease, viewer_hash: 'someone-else' })
  const view = render(<BotScreenPane bot={bot} />)
  await screen.findByRole('button', { name: /Take over/ })

  expect(screen.queryByRole('button', { name: /Copy from screen/ })).toBeNull()
  view.unmount()
})

it('leaves the local clipboard alone when nothing is copied on the screen', async () => {
  clipboardText = async () => ({ text: '' })
  const view = render(<BotScreenPane bot={bot} />)
  const button = await screen.findByRole('button', { name: /Copy from screen/ })
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))

  fireEvent.click(button)

  expect(await screen.findByText('Nothing copied')).toBeTruthy()
  expect(writeText).not.toHaveBeenCalled()
  view.unmount()
})

it('says so when the gateway refuses, without touching the clipboard', async () => {
  clipboardText = async () => Promise.reject(new Error('only the viewer in control of this screen can copy from it'))
  const view = render(<BotScreenPane bot={bot} />)
  const button = await screen.findByRole('button', { name: /Copy from screen/ })
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))

  fireEvent.click(button)

  expect(await screen.findByText('Copy failed')).toBeTruthy()
  expect(writeText).not.toHaveBeenCalled()
  view.unmount()
})
