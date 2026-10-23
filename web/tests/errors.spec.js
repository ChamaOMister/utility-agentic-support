import { test, expect } from '@playwright/test';
import { send, lastReply, fulfill, inProgress, interrupted } from './helpers.js';

test('409 shows the in-progress message, not a crash', async ({ page }) => {
  await page.route('**/messages', route => fulfill(route, {error: 'turn_in_progress', message: inProgress}, 409));
  await page.goto('/');
  await send(page, 'My bills');
  await expect(lastReply(page)).toHaveText(inProgress);
  await expect(page.getByLabel('Your message')).toBeEnabled();
});

test('interrupted reply offers explicit retry with a new id', async ({ page }) => {
  const requests = [];
  await page.route('**/messages', route => {
    requests.push(route.request().postDataJSON());
    return fulfill(route, {status: requests.length === 1 ? 'interrupted' : 'completed', reply: requests.length === 1 ? interrupted : 'Reply recovered.', trace: {tool_calls: [], model_calls: []}});
  });
  await page.goto('/');
  await send(page, 'I want to see my bills');
  await expect(lastReply(page)).toHaveText(interrupted);
  expect(requests).toHaveLength(1);
  await page.getByRole('button', {name: 'Try again', exact: true}).click();
  await expect(lastReply(page)).toHaveText('Reply recovered.');
  expect(requests).toHaveLength(2);
  expect(requests[0].client_message_id).not.toBe(requests[1].client_message_id);
  expect(requests[0].text).toBe(requests[1].text);
});

test('lost response retries the same client id without duplicate transcript entries', async ({ page }) => {
  const requests = [];
  await page.route('**/messages', async route => {
    requests.push(route.request().postDataJSON());
    if (requests.length === 1) {
      // Complete the real turn but drop its response: idempotent replay must recover it.
      await route.fetch();
      return route.abort('connectionreset');
    }
    return route.continue();
  });
  await page.goto('/');
  await send(page, 'I want to see my bills');
  await expect(lastReply(page)).toContainText('CPF');
  expect(requests).toHaveLength(2);
  expect(requests[0]).toEqual(requests[1]);
  await page.reload();
  await expect(page.getByTestId('user-message')).toHaveCount(1);
  await expect(page.getByTestId('assistant-reply')).toHaveCount(1);
});

test('debug drawer renders only explicit metadata fields, never nested payloads', async ({ page }) => {
  await page.route('**/messages', route => fulfill(route, {
    status: 'completed', reply: 'Synthetic customer message.',
    trace: {
      user_text: 'PRIVATE_USER_TEXT',
      tool_calls: [{name: 'list_bills', status: 'ok', error_code: null, attempts: 1, duration_ms: 12, record_count: 2, arguments: 'PRIVATE_ARGUMENTS', result: 'PRIVATE_RESULT'}],
      model_calls: [{kind: 'generation', model: 'demo-model', service_tier: 'default', input_tokens: 42, output_tokens: 12, estimated_cost_usd: '0.000123', outcome_class: 'completed', payload: 'PRIVATE_MODEL_PAYLOAD'}],
    },
  }));
  await page.goto('/');
  await send(page, 'PRIVATE_USER_TEXT');
  await page.getByText('Debug metadata', {exact: true}).click();
  const drawer = page.getByTestId('debug-metadata');
  await expect(drawer).toContainText('ac923faa-86ec-441f-ae91-91acac452721');
  for (const value of ['list_bills', 'demo-model', '0.000123', 'record_count', 'outcome_class']) await expect(drawer).toContainText(value);
  for (const value of ['PRIVATE_USER_TEXT', 'PRIVATE_ARGUMENTS', 'PRIVATE_RESULT', 'PRIVATE_MODEL_PAYLOAD', 'Synthetic customer']) await expect(drawer).not.toContainText(value);
});

test('health failure is visible and can be retried', async ({ page }) => {
  let unavailable = true;
  await page.route('**/api/health', route => unavailable ? fulfill(route, {}, 503) : route.continue());
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText("We couldn't connect");
  unavailable = false;
  await page.getByRole('button', {name: 'Try again', exact: true}).click();
  await expect(page.getByText('Offline mode — simulated replies')).toBeVisible();
  await send(page, 'Hello');
  await expect(lastReply(page)).toContainText('Vértice');
});

test('exhausted network retry stays recoverable and keeps the original id', async ({ page }) => {
  const requests = [];
  await page.route('**/messages', route => {
    requests.push(route.request().postDataJSON());
    return requests.length <= 2 ? route.abort('connectionreset') : fulfill(route, {
      status: 'completed', reply: 'Connection recovered.', trace: {tool_calls: [], model_calls: []},
    });
  });
  await page.goto('/');
  await send(page, 'My bills');
  await expect(lastReply(page)).toContainText("We couldn't connect");
  expect(requests).toHaveLength(2);
  await page.getByRole('button', {name: 'Try again', exact: true}).click();
  await expect(lastReply(page)).toHaveText('Connection recovered.');
  expect(requests).toHaveLength(3);
  expect(new Set(requests.map(r => r.client_message_id)).size).toBe(1);
  await expect(page.getByTestId('user-message')).toHaveCount(1);
});

test('lost response followed by in-progress replay keeps same-id recovery available', async ({ page }) => {
  const requests = [];
  let originalRequest;
  await page.route('**/messages', async route => {
    requests.push(route.request().postDataJSON());
    if (requests.length === 1) {
      // Drop the browser connection while its independent API request is still running.
      originalRequest = route.fetch();
      return route.abort('connectionreset');
    }
    if (requests.length <= 3) {
      // Deterministically emulate an active original turn on automatic and explicit replay.
      return fulfill(route, {error: 'turn_in_progress', message: inProgress}, 409);
    }
    await originalRequest;
    return route.continue(); // The real API must replay the completed original turn.
  });
  await page.goto('/');
  await send(page, 'I want to see my bills');
  await expect(lastReply(page)).toHaveText(inProgress);
  expect(requests).toHaveLength(2);
  const retry = page.getByRole('button', {name: 'Try again', exact: true});
  await expect(retry).toBeVisible();
  await retry.click();
  await expect(lastReply(page)).toHaveText(inProgress);
  await expect(retry).toBeVisible();
  await retry.click();
  await expect(lastReply(page)).toContainText('CPF');
  expect(requests).toHaveLength(4);
  for (const request of requests) expect(request).toEqual(requests[0]);
  await expect(page.getByTestId('user-message')).toHaveCount(1);
  await page.reload();
  await expect(page.getByTestId('user-message')).toHaveCount(1);
  await expect(page.getByTestId('assistant-reply')).toHaveCount(1);
  await expect(lastReply(page)).toContainText('CPF');
});
