import assert from 'node:assert/strict'
import http from 'node:http'

import { test } from 'vitest'

import { buildGatewayWsUrlWithTicket, configuredGatewayTokenTicketFailure } from './connection-config'
import { mintGatewayWsTicketWithSessionToken } from './gateway-ticket-transport'

type CapturedRequest = { body: string; headers: http.IncomingHttpHeaders; method?: string }

async function withTicketFixture(
  statusCode: number,
  run: (baseUrl: string, requests: CapturedRequest[]) => Promise<void>
) {
  const requests: CapturedRequest[] = []

  const server = http.createServer((request, response) => {
    const chunks: Buffer[] = []
    request.on('data', chunk => chunks.push(Buffer.from(chunk)))
    request.on('end', () => {
      requests.push({ body: Buffer.concat(chunks).toString('utf8'), headers: request.headers, method: request.method })
      response.writeHead(statusCode, { 'content-type': 'application/json' })
      response.end(statusCode >= 400 ? JSON.stringify({ detail: 'ticket rejected' }) : JSON.stringify({ ticket: `ticket-${requests.length}` }))
    })
  })

  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  assert.ok(address && typeof address === 'object')

  try {
    await run(`http://127.0.0.1:${address.port}`, requests)
  } finally {
    await new Promise<void>((resolve, reject) => server.close(error => (error ? reject(error) : resolve())))
  }
}

test('configured-token production ticket transport mints distinct initial and reconnect tickets with required headers', async () => {
  await withTicketFixture(200, async (baseUrl, requests) => {
    const token = 'persistent-gateway-secret'
    const headers = { 'X-Custom-Gateway-Header': 'custom-value' }
    const initialTicket = await mintGatewayWsTicketWithSessionToken(baseUrl, token, headers)
    const reconnectTicket = await mintGatewayWsTicketWithSessionToken(baseUrl, token, headers)
    const urls = [buildGatewayWsUrlWithTicket(baseUrl, initialTicket), buildGatewayWsUrlWithTicket(baseUrl, reconnectTicket)]

    assert.notEqual(initialTicket, reconnectTicket)
    assert.equal(requests.length, 2)

    for (const request of requests) {
      assert.equal(request.headers['x-hermes-session-token'], token)
      assert.equal(request.headers['x-custom-gateway-header'], 'custom-value')
      assert.equal(request.method, 'POST')
      assert.equal(request.body, '')
    }

    for (const url of urls) {
      assert.match(url, /\?ticket=ticket-[12]$/)
      assert.doesNotMatch(url, /persistent-gateway-secret|[?&]token=/)
    }
  })
})

test('configured-token production ticket transport preserves real HTTP 401/403 versus 503 status and guidance', async () => {
  for (const statusCode of [401, 403, 503]) {
    await withTicketFixture(statusCode, async baseUrl => {
      await assert.rejects(
        () => mintGatewayWsTicketWithSessionToken(baseUrl, 'persistent-gateway-secret'),
        (error: any) => {
          assert.equal(error.statusCode, statusCode)

          const classified: any = configuredGatewayTokenTicketFailure(
            error,
            'configured token rejected',
            'gateway unavailable'
          )

          assert.equal(classified.statusCode, statusCode)
          assert.equal(classified.needsConfiguredGatewayToken, statusCode === 401 || statusCode === 403 ? true : undefined)
          assert.match(classified.message, statusCode === 401 || statusCode === 403 ? /token rejected/ : /unavailable/)

          return true
        }
      )
    })
  }
})
