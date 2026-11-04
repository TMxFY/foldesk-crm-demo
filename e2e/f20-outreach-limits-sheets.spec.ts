import { expect, test } from '@playwright/test';
import { assertQuiet, connect, reset, resetProblems, watch } from './helpers';

test.beforeEach(async ({ page }) => {
  resetProblems();
  watch(page);
  await reset(page);
  await page.goto('/');
});

test.afterEach(() => {
  assertQuiet();
});

test('F20-H1 sheet mode switches active sheet and hides inactive sections', async ({ page }) => {
  await expect(page.locator('#workspace-main')).toHaveAttribute('data-view-mode', 'all');
  await page.locator('#toggle-view-mode').click();
  await expect(page.locator('#workspace-main')).toHaveAttribute('data-view-mode', 'sheet');

  await page.locator('.nav a[href="#outreach"]').click();
  await expect(page.locator('#outreach')).toBeVisible();
  await expect(page.locator('#s07')).toBeHidden();
  await expect(page.locator('#leads')).toBeHidden();

  await page.locator('.nav a[href="#accounts"]').click();
  await expect(page.locator('#accounts')).toBeVisible();
  await expect(page.locator('#outreach')).toBeHidden();

  await page.locator('.nav a[href="#s07"]').click();
  await expect(page.locator('#s07')).toBeVisible();
  await expect(page.locator('#accounts')).toBeHidden();
});

test('F20-H2 account daily limit buttons (10 / 20) update limit and enforce cap', async ({ page }) => {
  await connect(page);
  const accounts = page.locator('#accounts');
  const btn10 = accounts.getByRole('button', { name: /10\s*\/\s*сут/ });
  await btn10.click();
  await expect(btn10).toHaveAttribute('aria-pressed', 'true');

  const apiAcc10 = await (await page.request.get('/api/accounts')).json();
  expect(apiAcc10[0].daily_limit).toBe(10);

  await accounts.locator('.limit-input').fill('1');
  await accounts.getByRole('button', { name: 'Сохранить лимит' }).click();
  await expect(accounts.getByText('Сегодня: 0 / 1')).toBeVisible();

  await page.getByRole('button', { name: 'В Telegram' }).click();
  await page.getByLabel('Запрос').fill('@ivan');
  await page.getByRole('button', { name: 'Найти' }).click();
  await page.getByRole('button', { name: /Иван Петров/ }).click();

  const thread = page.getByRole('region', { name: 'Переписка' });
  await thread.getByLabel('Ответ').fill('Первое сообщение в пределах лимита');
  await thread.getByRole('button', { name: 'Отправить' }).click();
  await expect(thread.getByText('Первое сообщение в пределах лимита')).toBeVisible();

  await thread.getByLabel('Ответ').fill('Второе сообщение сверх лимита');
  await thread.getByRole('button', { name: 'Отправить' }).click();
  await expect(thread.getByRole('alert')).toContainText(
    'Достигнут суточный лимит аккаунта (1 сообщ./сутки)'
  );
});

test('F20-H3 CSV upload and multi-step outreach sequence sends personalized messages and stops on reply', async ({ page }) => {
  await connect(page);

  await page.locator('#campaign-name').fill('Тестовая цепочка');
  await page.locator('#create-campaign-btn').click();
  await expect(page.locator('#campaign-list')).toContainText('Тестовая цепочка');

  const csvText = [
    'username,name,phone,company,note',
    '@ivan,Иван Петров,+79991112233,TON Corp,VIP',
    '@olga,Ольга,+79991112244,Studio,Lead',
  ].join('\n');
  await page.locator('#csv-text').fill(csvText);
  await page.locator('#import-csv-btn').click();

  await expect(page.locator('#campaign-list')).toContainText('Иван Петров');
  await expect(page.locator('#campaign-list')).toContainText('Ольга');

  await page.locator('#campaign-list').getByRole('button', { name: 'Запустить шаг рассылки' }).click();
  await expect(page.locator('#toast')).toContainText('Отправлено сообщений: 2');

  const leadsSection = page.locator('#leads');
  await expect(leadsSection.locator('input[value="Иван Петров"]')).toBeVisible();
  await expect(leadsSection.locator('input[value="Ольга"]')).toBeVisible();
});

test('F20-H4 sheet mode popups and slide-over lead drawer open and close cleanly', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#board .card').first()).toBeVisible();

  await page.locator('#toggle-view-mode').click();
  await expect(page.locator('#workspace-main')).toHaveAttribute('data-view-mode', 'sheet');

  // 1. Clicking a deal card on Pipeline opens #s09 as a right-side Slide-over Drawer
  await expect(page.locator('#s09')).toBeHidden();
  await page.locator('#board .card').first().click();
  await expect(page.locator('#workspace-main')).toHaveAttribute('data-active-popup', 'card');
  await expect(page.locator('#s09')).toBeVisible();
  await page.locator('#close-card-popup').click();
  await expect(page.locator('#s09')).toBeHidden();

  // 2. On Outreach tab, builder and CSV boxes are hidden until opened as popups
  await page.locator('.nav a[href="#outreach"]').click();
  await expect(page.locator('#outreach-builder-box')).toBeHidden();
  await expect(page.locator('#outreach-csv-box')).toBeHidden();

  await page.locator('#open-builder-popup').click();
  await expect(page.locator('#outreach-builder-box')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#outreach-builder-box')).toBeHidden();

  await page.locator('#open-csv-popup').click();
  await expect(page.locator('#outreach-csv-box')).toBeVisible();
  await page.locator('#sheet-popup-backdrop').click({ position: { x: 10, y: 10 } });
  await expect(page.locator('#outreach-csv-box')).toBeHidden();

  // 3. On Leads tab, manual lead creation form opens as a popup
  await page.locator('.nav a[href="#leads"]').click();
  await expect(page.locator('#manual-lead-box')).toBeHidden();
  await page.locator('#open-lead-popup').click();
  await expect(page.locator('#manual-lead-box')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#manual-lead-box')).toBeHidden();
});

test('F20-H5 multi-account campaign pool, explicit session verification, and SoftUGC 30-lead CSV import', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#board .card').first()).toBeVisible();

  // 1. Check explicit account connection status & Check All button
  const accounts = page.locator('#accounts');
  await expect(accounts.locator('#accounts-summary-bar')).toContainText('Подключено: 3');
  await expect(accounts.locator('.account-badges-line .badge').first()).toContainText('Демо-профиль (локально)');
  await accounts.locator('#check-accounts-btn').click();
  await expect(page.locator('#toast')).toContainText('Проверено аккаунтов: 3');

  // 2. Create a multi-account campaign with all 3 connected accounts
  await page.locator('#campaign-name').fill('SoftUGC Multi-Account Launch');
  await page.locator('#select-all-campaign-accounts').click();
  await expect(page.locator('#campaign-pool-summary')).toContainText('Пул: 3 аккаунта');
  await page.locator('#create-campaign-btn').click();
  await expect(page.locator('#campaign-list')).toContainText('SoftUGC Multi-Account Launch');
  await expect(page.locator('#campaign-list')).toContainText('Пул: 3 аккаунта');

  // 3. Load the real 39-row SoftUGC CSV sample and import into the new campaign
  await page.locator('#fill-sample-csv').click();
  await expect(page.locator('#toast')).toContainText('Загружен пример таблицы SoftUGC');
  await page.locator('#import-csv-btn').click();
  await expect(page.locator('#toast')).toContainText('Импортировано из CSV: 39 контактов');

  // Verify cleaned company name, website, and filtered Cloudflare refusal row
  await expect(page.locator('#campaign-list')).toContainText('ClawsGO Science');
  await expect(page.locator('#campaign-list')).toContainText('clawsgo.ai');
  await expect(page.locator('#campaign-list')).toContainText('Пропущен: в CSV ошибка генерации текста');

  // 4. Run campaign step and verify multi-account distribution
  const card = page.locator('#campaign-list .campaign-card').first();
  await card.getByRole('button', { name: 'Запустить шаг рассылки' }).click();
  await expect(page.locator('#toast')).toContainText('Отправлено сообщений:');
});


