import { expect } from '@playwright/test';
export const lastReply = page => page.getByTestId('assistant-reply').last();
export async function send(page, text) {
  const count = await page.getByTestId('assistant-reply').count();
  await page.getByLabel('Sua mensagem').fill(text);
  await page.getByRole('button', {name: 'Enviar mensagem', exact: true}).click();
  await expect(page.getByTestId('assistant-reply')).toHaveCount(count + 1);
}
export const inProgress = 'Ainda estou processando sua mensagem anterior. Aguarde um momento.';
export const interrupted = 'Não foi possível concluir sua mensagem. Envie novamente para tentar outra vez.';
export function fulfill(route, body, status = 200) {
  return route.fulfill({status, contentType: 'application/json', headers: {'X-Correlation-ID': 'ac923faa-86ec-441f-ae91-91acac452721'}, body: JSON.stringify(body)});
}
