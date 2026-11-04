import { expect, test } from '@playwright/test';

test('landing opens the desk', async ({ page }) => {
  await page.goto('/landing');
  await expect(page.getByRole('heading', { level: 1, name: 'Один стол для своих чатов' })).toBeVisible();
  const shot = page.getByRole('img', { name: 'Стол Foldesk: входящие и карточка' });
  await expect(shot).toBeVisible();
  const src = await shot.getAttribute('src');
  expect(src).toBe('/static/landing-desk.png');
  const file = await page.request.get(src!);
  expect(file.status()).toBe(200);
  expect((await file.body()).byteLength).toBeGreaterThan(1000);
  await page.getByRole('link', { name: 'Открыть стол' }).first().click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole('button', { name: 'Войти через бота' })).toBeVisible();
});
