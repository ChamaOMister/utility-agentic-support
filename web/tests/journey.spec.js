import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { send, lastReply } from './helpers.js';
const [A, B] = JSON.parse(readFileSync(new URL('../../data/synthetic/personas.json', import.meta.url)));
const credentials = p => `${p.cpf}, ${p.birth_date.split('-').reverse().join('/')}, telefone ${p.phone.slice(-4)}`;

test('offline journey: bills → verify → summary, persisted on reload', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('Modo offline — respostas simuladas')).toBeVisible();
  await expect(page.getByRole('heading', {name: 'Dados de demonstração'})).toBeVisible();
  await expect(page.getByText('dados sintéticos', {exact: true})).toBeVisible();
  await send(page, 'Quero ver minhas faturas');
  await expect(lastReply(page)).toContainText('CPF');
  await send(page, credentials(A));
  await expect(lastReply(page)).toContainText('R$ 196,63');
  await expect(lastReply(page)).toContainText('R$ 221,44');
  const reply = await lastReply(page).textContent();
  await page.reload();
  await expect(lastReply(page)).toHaveText(reply);
  await expect(page.getByTestId('user-message')).toHaveCount(2);
  await page.getByText('Debug metadata', {exact: true}).click();
  const drawer = page.getByTestId('debug-metadata');
  await expect(drawer).toContainText('authenticate_customer');
  await expect(drawer).toContainText('record_count');
  for (const forbidden of [A.first_name, A.cpf, A.account_number, A.birth_date, A.phone.slice(-4), credentials(A)]) {
    await expect(drawer).not.toContainText(forbidden);
  }
});

test('empty open bills remain explicit on a narrow screen', async ({ page }) => {
  await page.setViewportSize({width: 390, height: 844});
  await page.goto('/');
  await send(page, 'Quero ver minhas faturas em aberto');
  await send(page, credentials(B));
  await expect(lastReply(page)).toContainText('Não encontrei faturas em aberto.');
  await expect(lastReply(page)).toBeInViewport();
  await page.reload();
  await expect(lastReply(page)).toContainText('Não encontrei faturas em aberto.');
  await expect(lastReply(page)).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
