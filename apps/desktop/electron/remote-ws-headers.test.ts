import http from 'node:http'
import { WebSocketServer } from 'ws'

import { describe, expect, it, vi } from 'vitest'

import {
  applyRemoteRequestHeaders,
  createRegistryGatewayWsUrlHandler,
  createRemoteWsHeaderStore,
  type RegistryGatewayWsConnection
} from './remote-ws-headers'
import { mintGatewayWsTicketWithSessionToken } from './gateway-ticket-transport'

const accessHeaders = {
  'CF-Access-Client-Id': 'client-id',
  'CF-Access-Client-Secret': 'client-secret'
}

function createHarness(connection: RegistryGatewayWsConnection) {
  const store = createRemoteWsHeaderStore()
  const ensureBackend = vi.fn(async () => connection)
  const mintConfiguredTokenTicket = vi.fn(async () => 'configured-token-ticket')
  const mintTicket = vi.fn(async () => 'fresh-ticket')

  const handler = createRegistryGatewayWsUrlHandler({
    ensureBackend,
    mintConfiguredTokenTicket,
    mintTicket,
    buildTicketUrl: baseUrl => `${baseUrl.replace(/^https:/, 'wss:')}/api/ws?region=us&ticket=fresh-ticket&profile=old`,
    rememberHeaders: store.remember
  })

  return { ensureBackend, handler, mintConfiguredTokenTicket, mintTicket, store }
}

function expectRequestHeaders(
  store: ReturnType<typeof createRemoteWsHeaderStore>,
  url: string,
  expected: Record<string, string> | undefined
) {
  const callback = vi.fn()

  applyRemoteRequestHeaders({ url, requestHeaders: { Origin: 'app://hermes' } }, callback, store.headersFor)

  expect(callback).toHaveBeenCalledOnce()
  expect(callback).toHaveBeenCalledWith(expected ? { requestHeaders: { Origin: 'app://hermes', ...expected } } : {})
}

function expectNoHeadersForNearbyUrls(store: ReturnType<typeof createRemoteWsHeaderStore>, exactUrl: string) {
  const exact = new URL(exactUrl)
  const unscoped = new URL(exact)
  unscoped.searchParams.delete('profile')
  const sibling = new URL(exact)
  sibling.pathname = '/api/ws/sibling'
  const otherProfile = new URL(exact)
  otherProfile.searchParams.set('profile', 'analysis')
  const otherCredential = new URL(exact)

  if (otherCredential.searchParams.has('ticket')) {
    otherCredential.searchParams.set('ticket', 'other-ticket')
  } else {
    otherCredential.searchParams.set('token', 'other-token')
  }

  const reordered = new URL(exact)
  const entries = [...reordered.searchParams.entries()].reverse()
  reordered.search = ''

  for (const [name, value] of entries) {
    reordered.searchParams.append(name, value)
  }

  for (const url of [unscoped, sibling, otherProfile, otherCredential, reordered]) {
    expect(store.headersFor(url.toString())).toEqual({})
    expectRequestHeaders(store, url.toString(), undefined)
  }
}

describe('registry gateway WebSocket headers', () => {
  it('evicts the least recently accessed exact URL', () => {
    const store = createRemoteWsHeaderStore(2)
    const firstUrl = 'wss://gateway.example/api/ws?token=first&profile=research'
    const secondUrl = 'wss://gateway.example/api/ws?token=second&profile=research'
    const thirdUrl = 'wss://gateway.example/api/ws?token=third&profile=research'

    store.remember(firstUrl, accessHeaders)
    store.remember(secondUrl, accessHeaders)
    expect(store.headersFor('wss://gateway.example/api/ws?token=missing&profile=research')).toEqual({})
    expect(store.headersFor(firstUrl)).toEqual(accessHeaders)

    store.remember(thirdUrl, accessHeaders)

    expect(store.headersFor(firstUrl)).toEqual(accessHeaders)
    expect(store.headersFor(secondUrl)).toEqual({})
    expect(store.headersFor(thirdUrl)).toEqual(accessHeaders)
  })

  it('updates headers without changing insertion recency', () => {
    const store = createRemoteWsHeaderStore(2)
    const firstUrl = 'wss://gateway.example/api/ws?token=first'
    const secondUrl = 'wss://gateway.example/api/ws?token=second'
    const thirdUrl = 'wss://gateway.example/api/ws?token=third'

    store.remember(firstUrl, { 'CF-Access-Client-Id': 'old-client-id' })
    store.remember(secondUrl, accessHeaders)
    store.remember(firstUrl, { 'CF-Access-Client-Id': 'updated-client-id' })
    store.remember(thirdUrl, accessHeaders)

    expect(store.headersFor(firstUrl)).toEqual({})
    expect(store.headersFor(secondUrl)).toEqual(accessHeaders)
    expect(store.headersFor(thirdUrl)).toEqual(accessHeaders)
  })

  it('configured-token registry reconnect mints fresh scoped tickets through the production transport', async () => {
    const requests: http.IncomingHttpHeaders[] = []
    const unusedTickets = new Set<string>()
    const server = http.createServer((request, response) => {
      requests.push(request.headers)
      const ticket = `one-use-ticket-${requests.length}`
      unusedTickets.add(ticket)
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ ticket }))
    })
    const socketServer = new WebSocketServer({ noServer: true })
    server.on('upgrade', (request, socket, head) => {
      const ticket = new URL(request.url || '/', 'http://127.0.0.1').searchParams.get('ticket')

      if (!ticket || !unusedTickets.delete(ticket)) {
        socket.end('HTTP/1.1 401 Unauthorized\r\n\r\n')
        return
      }

      socketServer.handleUpgrade(request, socket, head, webSocket => {
        webSocket.send('ready')
      })
    })
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const address = server.address()
    expect(address && typeof address === 'object').toBeTruthy()
    const baseUrl = `http://127.0.0.1:${(address as any).port}`
    const storedConnections = new Map<string, RegistryGatewayWsConnection>([
      [
        'remote-one',
        {
          authMode: 'token',
          baseUrl,
          headers: { 'X-Remote-Scope': 'one' },
          mode: 'remote',
          profile: 'research',
          remoteKind: 'url',
          sharedRemote: true,
          token: 'remote-one-secret',
          wsUrl: 'ws://127.0.0.1/stale?ticket=already-consumed'
        }
      ],
      [
        'remote-two',
        {
          authMode: 'token',
          baseUrl,
          headers: accessHeaders,
          mode: 'remote',
          profile: 'research',
          remoteKind: 'url',
          sharedRemote: true,
          token: 'remote-two-secret',
          wsUrl: 'ws://127.0.0.1/stale?ticket=already-consumed'
        }
      ]
    ])
    const store = createRemoteWsHeaderStore()
    const ensureBackend = vi.fn(async (connectionId: unknown) => storedConnections.get(String(connectionId))!)
    const handler = createRegistryGatewayWsUrlHandler({
      ensureBackend,
      mintConfiguredTokenTicket: (connection: any) =>
        mintGatewayWsTicketWithSessionToken(connection.baseUrl, connection.token, connection.headers),
      mintTicket: async () => {
        throw new Error('OAuth ticket transport must not receive configured-token connections')
      },
      buildTicketUrl: (url, ticket) => `${url.replace('http:', 'ws:')}/api/ws?ticket=${ticket}`,
      rememberHeaders: store.remember
    })
    const connectOnce = (url: string) =>
      new Promise<void>((resolve, reject) => {
        const socket = new WebSocket(url)
        socket.addEventListener('message', () => {
          socket.close()
          resolve()
        })
        socket.addEventListener('error', () => reject(new Error('one-use WebSocket upgrade failed')))
      })

    try {
      const initial = await handler({ connectionId: 'remote-two', profile: 'research' })
      const reconnect = await handler({ connectionId: 'remote-two', profile: 'research' })

      await connectOnce(initial)
      await connectOnce(reconnect)

      expect(ensureBackend).toHaveBeenNthCalledWith(1, 'remote-two', 'research')
      expect(ensureBackend).toHaveBeenNthCalledWith(2, 'remote-two', 'research')
      expect(requests).toHaveLength(2)
      for (const request of requests) {
        expect(request['x-hermes-session-token']).toBe('remote-two-secret')
        expect(request['cf-access-client-id']).toBe('client-id')
        expect(request['cf-access-client-secret']).toBe('client-secret')
        expect(request['x-remote-scope']).toBeUndefined()
      }
      expect(initial).toContain('ticket=one-use-ticket-1')
      expect(reconnect).toContain('ticket=one-use-ticket-2')
      expect(initial).not.toContain('remote-two-secret')
      expect(reconnect).not.toContain('remote-two-secret')
      expect(initial).not.toContain('token=')
      expect(reconnect).not.toContain('token=')
      expect(store.headersFor(initial)).toEqual(accessHeaders)
      expect(store.headersFor(reconnect)).toEqual(accessHeaders)
      expectRequestHeaders(store, reconnect, accessHeaders)
      expectNoHeadersForNearbyUrls(store, reconnect)
      expect(unusedTickets).toEqual(new Set())
    } finally {
      socketServer.close()
      await new Promise<void>((resolve, reject) => server.close(error => (error ? reject(error) : resolve())))
    }
  })

  it('OAuth path binds headers to the exact fresh profile scoped URL', async () => {
    const { handler, mintConfiguredTokenTicket, mintTicket, store } = createHarness({
      authMode: 'oauth',
      baseUrl: 'https://gateway.example',
      wsUrl: 'wss://gateway.example/api/ws?ticket=stale',
      headers: accessHeaders,
      profile: 'research',
      sharedRemote: true
    })

    const result = await handler({ connectionId: 'cloud-one', profile: 'research' })
    const expectedUrl = 'wss://gateway.example/api/ws?region=us&ticket=fresh-ticket&profile=research'

    expect(result).toBe(expectedUrl)
    expect(mintTicket).toHaveBeenCalledOnce()
    expect(mintTicket).toHaveBeenCalledWith('https://gateway.example', accessHeaders)
    expect(mintConfiguredTokenTicket).not.toHaveBeenCalled()
    expect(store.headersFor(result)).toEqual(accessHeaders)
    expectRequestHeaders(store, result, accessHeaders)
    expectNoHeadersForNearbyUrls(store, result)
  })

  it('sharedRemote false preserves the original URL and exact header behavior', async () => {
    const { handler, mintConfiguredTokenTicket, store } = createHarness({
      authMode: 'token',
      baseUrl: 'https://gateway.example',
      mode: 'local',
      wsUrl: 'wss://gateway.example/api/ws?trace=one&token=secret',
      headers: accessHeaders,
      profile: 'research',
      sharedRemote: false
    })

    const result = await handler({ connectionId: 'remote-one', profile: 'research' })

    expect(result).toBe('wss://gateway.example/api/ws?trace=one&token=secret')
    expect(mintConfiguredTokenTicket).not.toHaveBeenCalled()
    expect(store.headersFor(result)).toEqual(accessHeaders)
    expectRequestHeaders(store, result, accessHeaders)
    expect(store.headersFor('wss://gateway.example/api/ws?token=secret&trace=one')).toEqual({})
  })
})
