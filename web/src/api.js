// The only browser request retry policy. HTTP errors are never network retries.
export class ApiError extends Error {
  constructor(status, body) { super(body?.error ?? 'request_failed'); this.status = status; this.body = body; }
}
async function request(path, options = {}) {
  const response = await fetch(path, {credentials: 'same-origin', ...options});
  const body = await response.json();
  if (!response.ok) throw new ApiError(response.status, body);
  return {body, correlationId: response.headers.get('X-Correlation-ID')};
}
export const getHealth = async () => (await request('/api/health')).body;
export const getDemoCustomers = async () => (await request('/api/demo/customers')).body;
export const getConversation = async id => (await request(`/api/conversations/${encodeURIComponent(id)}`)).body;
export const createConversation = async () => (await request('/api/conversations', {
  method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
})).body.conversation_id;

export async function sendMessage(conversationId, text, clientMessageId) {
  for (let attempt = 0; attempt < 2; attempt++) {
    let response;
    try {
      response = await fetch(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, {
        method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text, client_message_id: clientMessageId}),
        signal: AbortSignal.timeout(55000),
      });
    } catch (error) {
      if (attempt === 0) continue;
      throw error;
    }
    const body = await response.json();
    const correlationId = response.headers.get('X-Correlation-ID');
    if (response.status === 409) return {reply: body.message, status: 'conflict', correlationId};
    if (!response.ok) throw new ApiError(response.status, body);
    return {...body, correlationId, retryWithNewId: body.status === 'interrupted'};
  }
}
