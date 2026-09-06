import http from 'node:http'
import https from 'node:https'

function httpStatusError(statusCode: number | undefined, message: string) {
  const error: any = new Error(`${statusCode}: ${message}`)
  error.statusCode = statusCode
  return error
}

/**
 * Mint one WebSocket ticket using a configured dashboard session token. This
 * production transport keeps HTTP status structured so callers can distinguish
 * a rejected token from a server or network failure.
 */
async function mintGatewayWsTicketWithSessionToken(baseUrl: string, token: string, headers: Record<string, string> = {}) {
  const url = new URL(`${String(baseUrl).replace(/\/+$/, '')}/api/auth/ws-ticket`)
  const client = url.protocol === 'https:' ? https : http

  return new Promise<string>((resolve, reject) => {
    const request = client.request(
      url,
      {
        method: 'POST',
        headers: {
          ...headers,
          'Content-Type': 'application/json',
          'X-Hermes-Session-Token': token
        }
      },
      response => {
        const chunks: Buffer[] = []
        response.on('error', reject)
        response.on('data', chunk => chunks.push(Buffer.from(chunk)))
        response.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8')

          if ((response.statusCode || 500) >= 400) {
            reject(httpStatusError(response.statusCode, text || response.statusMessage || 'HTTP request failed'))
            return
          }

          try {
            const ticket = JSON.parse(text)?.ticket
            if (!ticket || typeof ticket !== 'string') {
              reject(new Error('Gateway did not return a WS ticket.'))
              return
            }
            resolve(ticket)
          } catch {
            reject(new Error(`Invalid JSON from ${url} (status ${response.statusCode}): ${text.slice(0, 200)}`))
          }
        })
      }
    )

    request.on('error', reject)
    request.end()
  })
}

export { mintGatewayWsTicketWithSessionToken }
