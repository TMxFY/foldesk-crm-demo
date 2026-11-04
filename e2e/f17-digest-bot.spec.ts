import { expect, test } from '@playwright/test';
import { assertQuiet, reset, resetProblems, watch } from './helpers';

test.beforeEach(async ({ page }) => {
  resetProblems();
  watch(page);
  await reset(page);
  await page.goto('/');
});

test.afterEach(() => {
  assertQuiet();
});

test('F17 digest button succeeds only after accepted send', async ({ page }) => {
  const digest = page.getByRole('region', { name: 'Напоминания' });
  const button = digest.getByRole('button', { name: 'Прислать сводку' });
  const status = page.locator('#digest-send-status');

  await page.request.post('/api/test/digest-transport', { data: { mode: 'fail' } });
  await button.click();
  await expect(status).toBeVisible();
  await expect(status).not.toHaveText('Дайджест отправлен');
  await expect(status).toContainText('Telegram не принял сообщение');

  let release = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route('**/api/digest/send', async (route) => {
    await gate;
    await route.continue();
  });
  await page.request.post('/api/test/digest-transport', { data: { mode: 'ok' } });
  await button.click();
  await expect(status).toHaveText('');
  release();
  await expect(status).toHaveText('Дайджест отправлен');
  await expect(page.locator('#toast')).toHaveText('Дайджест отправлен');
});
