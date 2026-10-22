import { randomInt } from 'node:crypto';
import { test, expect } from '@playwright/test';
import { send, lastReply } from './helpers.js';

test('three wrong attempts lock with identical generic messages', async ({ page }) => {
  // A fresh non-persona identifier avoids shared lockouts in the persistent demo DB.
  const identifier = `9${randomInt(100000000, 999999999)}`;
  await page.goto('/');
  const wrong = `${identifier}, 01/01/1900, telefone 0000`;
  for (let i = 0; i < 4; i++) {
    await send(page, wrong);
    await expect(lastReply(page)).toHaveText('Não foi possível confirmar seus dados. Verifique as informações e tente novamente mais tarde.');
  }
  await page.getByText('Debug metadata', {exact: true}).click();
  await expect(page.getByTestId('debug-metadata')).toContainText('locked');
});
