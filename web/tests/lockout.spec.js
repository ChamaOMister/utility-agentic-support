import { randomInt } from 'node:crypto';
import { test, expect } from '@playwright/test';
import { send, lastReply } from './helpers.js';

test('three wrong attempts lock with identical generic messages', async ({ page }) => {
  // A fresh non-persona identifier avoids shared lockouts in the persistent demo DB.
  const identifier = `9${randomInt(100000000, 999999999)}`;
  await page.goto('/');
  const wrong = `${identifier}, born 1900-01-01, phone 0000`;
  for (let i = 0; i < 4; i++) {
    await send(page, wrong);
    await expect(lastReply(page)).toHaveText("We couldn't confirm your details. Please check the information and try again later.");
  }
  await page.getByText('Debug metadata', {exact: true}).click();
  await expect(page.getByTestId('debug-metadata')).toContainText('locked');
});
