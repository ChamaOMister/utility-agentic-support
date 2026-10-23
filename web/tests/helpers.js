import { expect } from '@playwright/test';
export const lastReply = page => page.getByTestId('assistant-reply').last();
export async function send(page, text) {
  const count = await page.getByTestId('assistant-reply').count();
  await page.getByLabel('Your message').fill(text);
  await page.getByRole('button', {name: 'Send message', exact: true}).click();
  await expect(page.getByTestId('assistant-reply')).toHaveCount(count + 1);
}
export const inProgress = "I'm still working on your previous message. Please wait a moment.";
export const interrupted = "We couldn't finish your message. Send it again to retry.";
export function fulfill(route, body, status = 200) {
  return route.fulfill({status, contentType: 'application/json', headers: {'X-Correlation-ID': 'ac923faa-86ec-441f-ae91-91acac452721'}, body: JSON.stringify(body)});
}
