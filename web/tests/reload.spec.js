import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { send, lastReply } from './helpers.js';

// Reloading while a real turn is in progress (final review I2). A persona-scoped list_bills
// `timeout` fault on the Compose mock billing keeps the server turn in progress for ~8 s.
const E = JSON.parse(readFileSync(new URL('../../data/synthetic/personas.json', import.meta.url)))[4];
const credentials = p => `${p.cpf}, ${p.birth_date.split('-').reverse().join('/')}, telefone ${p.phone.slice(-4)}`;
const billing = 'http://127.0.0.1:8001';

function faultHeaders() {
  const line = readFileSync(new URL('../../.env', import.meta.url), 'utf8')
    .split('\n').find(l => l.startsWith('FAULT_ADMIN_KEY='));
  if (!line) throw new Error('FAULT_ADMIN_KEY missing from .env: run scripts/init-env.sh');
  return {'X-Fault-Admin-Key': line.slice('FAULT_ADMIN_KEY='.length).trim()};
}

// Fault rules are process-wide on mock billing: keep these tests from overlapping each other.
test.describe.configure({mode: 'serial'});
test.afterEach(async ({request}) => {
  expect((await request.delete(`${billing}/_dev/faults`, {headers: faultHeaders()})).status()).toBe(204);
});

async function reloadMidTurn(page, request) {
  test.setTimeout(60000);
  const health = await (await request.get('/api/health')).json();
  await page.goto('/');
  await send(page, 'Quero ver minhas faturas');
  await send(page, credentials(E));
  await expect(lastReply(page)).toContainText('R$');
  const put = await request.put(`${billing}/_dev/faults`, {headers: faultHeaders(),
    data: {rules: [{route_name: 'list_bills', customer_ref: E.customer_ref, effect: 'timeout'}]}});
  expect(put.status()).toBe(204);

  const posts = [];
  let original;
  let reached;
  const routed = new Promise(resolve => {reached = resolve;});
  await page.route('**/messages', async route => {
    posts.push(route.request().postDataJSON());
    if (!original) {
      // The real turn runs on the server independently of this page; its response is never
      // delivered because the page reloads first.
      original = route.fetch();
      original.catch(() => {});
      reached();
      return;
    }
    await original; // A recovery request reaches the API only after the original turn ends.
    return route.continue();
  });
  await page.getByLabel('Sua mensagem').fill('Minhas faturas');
  await page.getByRole('button', {name: 'Enviar mensagem', exact: true}).click();
  await routed;
  const cid = await page.evaluate(() => localStorage.getItem('vertice-conversation-id'));
  await expect.poll(async () => (await (await page.request.get(`/api/conversations/${cid}`)).json()).turns.at(-1).status)
    .toBe('in_progress');
  await page.reload();
  await expect(page.getByTestId('user-message')).toHaveCount(3);
  await expect(page.getByTestId('user-message').last()).toHaveText('Minhas faturas');
  await expect(page.getByTestId('pending-reply')).toHaveText(health.ui.sending);
  await expect(page.getByRole('button', {name: health.ui.retry, exact: true})).toBeVisible();
  return {health, posts};
}

test('reload mid-turn: pending turn offers same-id recovery that shows the reply', async ({ page, request }) => {
  const {health, posts} = await reloadMidTurn(page, request);
  const replayed = page.waitForResponse(r => r.url().endsWith('/messages') && r.request().method() === 'POST');
  await page.getByRole('button', {name: health.ui.retry, exact: true}).click();
  expect((await replayed).status()).toBe(200);
  await expect(lastReply(page)).toHaveText(health.ui.unavailable); // the API's upstream_unavailable reply
  await expect(page.getByTestId('pending-reply')).toHaveCount(0);
  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]); // same client_message_id and same text
  await expect(page.getByTestId('user-message')).toHaveCount(3);
  await page.reload();
  await expect(page.getByTestId('user-message')).toHaveCount(3);
  await expect(page.getByTestId('assistant-reply')).toHaveCount(3);
  await expect(lastReply(page)).toHaveText(health.ui.unavailable);
});

test('reload mid-turn: the reply appears on its own once the turn completes', async ({ page, request }) => {
  const {health, posts} = await reloadMidTurn(page, request);
  await expect(page.getByTestId('assistant-reply')).toHaveCount(3, {timeout: 25000});
  await expect(lastReply(page)).toHaveText(health.ui.unavailable);
  await expect(page.getByTestId('pending-reply')).toHaveCount(0);
  await expect(page.getByRole('button', {name: health.ui.retry, exact: true})).toHaveCount(0);
  expect(posts).toHaveLength(1); // recovered by reading the transcript, never by resending
});
