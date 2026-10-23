import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { send, lastReply } from './helpers.js';
const [A] = JSON.parse(readFileSync(new URL('../../data/synthetic/personas.json', import.meta.url)));
const formatCPF = cpf => cpf.replace(/(\d{3})(\d{3})(\d{3})(\d{2})/, '$1.$2.$3-$4');

test('verify-identity form: calendar date, then the bills are listed', async ({ page }) => {
  await page.goto('/');
  await send(page, 'I want to see my bills');
  await expect(lastReply(page)).toContainText('Verify my identity');
  await page.getByRole('button', {name: 'Verify my identity', exact: true}).click();
  const form = page.getByRole('form', {name: 'Verify my identity'});
  await form.getByLabel('CPF or account number').fill(formatCPF(A.cpf));
  await expect(form.getByLabel('Date of birth')).toHaveAttribute('type', 'date');
  await form.getByLabel('Date of birth').fill(A.birth_date);
  await form.getByLabel('Last 4 digits of your phone').fill(A.phone.slice(-4));
  const count = await page.getByTestId('assistant-reply').count();
  await form.getByRole('button', {name: 'Verify', exact: true}).click();
  await expect(page.getByTestId('assistant-reply')).toHaveCount(count + 1);
  await expect(page.getByTestId('user-message').last()).toHaveText(
    `My CPF or account number is ${A.cpf}, my date of birth is ${A.birth_date} and the last 4 digits of my phone are ${A.phone.slice(-4)}.`);
  await expect(lastReply(page)).toContainText('R$ 196.63');
  await expect(form).toHaveCount(0);
});

test('verify-identity form explains invalid fields and sends nothing', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', {name: 'Verify my identity', exact: true}).click();
  const form = page.getByRole('form', {name: 'Verify my identity'});
  await form.getByLabel('CPF or account number').fill('123');
  await form.getByLabel('Last 4 digits of your phone').fill('12a');
  await form.getByRole('button', {name: 'Verify', exact: true}).click();
  await expect(form.getByText('Enter an 11-digit CPF or a 10-digit account number.')).toBeVisible();
  await expect(form.getByText('Pick your date of birth from the calendar.')).toBeVisible();
  await expect(form.getByText('Enter exactly 4 digits.')).toBeVisible();
  await expect(form.getByLabel('CPF or account number')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.getByTestId('user-message')).toHaveCount(0);
  await form.getByRole('button', {name: 'Cancel', exact: true}).click();
  await expect(form).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Verify my identity', exact: true})).toBeVisible();
});

test('verify-identity form fits a narrow screen', async ({ page }) => {
  await page.setViewportSize({width: 390, height: 844});
  await page.goto('/');
  await page.getByRole('button', {name: 'Verify my identity', exact: true}).click();
  await expect(page.getByRole('form', {name: 'Verify my identity'}).getByRole('button', {name: 'Verify', exact: true})).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('a typed impossible date is explained, without format codes', async ({ page }) => {
  await page.goto('/');
  await send(page, `CPF ${A.cpf}, born 1989-02-89, phone ${A.phone.slice(-4)}`);
  await expect(lastReply(page)).toContainText("February 1989 doesn't have a day 89");
  await expect(lastReply(page)).not.toContainText('AAAA');
  await expect(lastReply(page)).not.toContainText('YYYY');
});
