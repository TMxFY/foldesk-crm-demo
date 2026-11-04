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

test('F21-H1 Session + JSON (ZIP) account import adds connected account with proxy and access metadata', async ({ page }) => {
  await page.locator('#toggle-view-mode').click();
  await expect(page.locator('#workspace-main')).toHaveAttribute('data-view-mode', 'sheet');

  await page.locator('.nav a[data-sheet="accounts"]').click();
  await expect(page.locator('#accounts')).toBeVisible();

  await page.locator('#open-login-popup').click();
  await expect(page.locator('#account-auth-box')).toBeVisible();

  await page.locator('#auth-tab-btn-session').click();
  await expect(page.locator('#auth-pane-session')).toBeVisible();

  await page.locator('#session-proxy-select').selectOption('eu_residential');
  await page.locator('#session-access-select').selectOption('workspace');
  await page.locator('#fill-sample-session-btn').click();
  await expect(page.locator('#session-import-text')).toHaveValue(/foldesk_outreach_pro/);

  await page.locator('#import-session-btn').click();
  await expect(page.locator('#account-list')).toContainText('Foldesk Session');
  await expect(page.locator('#account-list')).toContainText('@foldesk_outreach_pro');
  await expect(page.locator('#account-list')).toContainText('Подключён');
});

test('F21-H2 Outreach schedule modal, Leads from CRM enrollment, and CSV column mapping preview', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#board .card').first()).toBeVisible();

  await page.locator('#toggle-view-mode').click();
  await expect(page.locator('#workspace-main')).toHaveAttribute('data-view-mode', 'sheet');

  await page.locator('.nav a[data-sheet="outreach"]').click();
  await expect(page.locator('#outreach')).toBeVisible();

  // 1. Sending Schedule popup (#outreach-open-schedule-btn & #schedule-form)
  await page.locator('#outreach-open-schedule-btn').click();
  await expect(page.locator('#outreach-schedule-box')).toBeVisible();
  await page.locator('#schedule-timezone').selectOption('Europe/Moscow');
  await page.locator('#schedule-form').getByRole('button', { name: 'Сохранить расписание' }).click();
  await expect(page.locator('#toast')).toContainText('Расписание рассылок сохранено');
  await expect(page.locator('#schedule-summary-badge')).toContainText('Europe/Moscow');

  // 2. Enroll leads from CRM (#outreach-open-from-leads-btn)
  await page.locator('#outreach-open-from-leads-btn').click();
  await expect(page.locator('#outreach-from-leads-box')).toBeVisible();
  await expect(page.locator('#from-leads-checklist .from-leads-item').first()).toBeVisible();
  await page.locator('#add-from-leads-btn').click();
  await expect(page.locator('#toast')).toContainText('Добавлено в рассылку из CRM:');

  // 3. CSV Import popup + Sample CSV + Column mapping & preview (#csv-mapping-preview-box)
  await page.locator('#open-csv-popup').click();
  await expect(page.locator('#outreach-csv-box')).toBeVisible();
  await page.locator('#fill-sample-csv').click();
  await expect(page.locator('#csv-mapping-preview-box')).toBeVisible();
  await expect(page.locator('#csv-preview-count-badge')).toContainText('39 строк');
  await expect(page.locator('#csv-map-username')).toHaveValue('Контакт');
  await expect(page.locator('#csv-map-company')).toHaveValue('Название');
  await expect(page.locator('#csv-preview-grid .csv-preview-row').first()).toBeVisible();
});

test('F21-H3 Live Tools: Phone to Telegram converter and Telegram Group Parser', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#board .card').first()).toBeVisible();

  await page.locator('#toggle-view-mode').click();
  await expect(page.locator('#workspace-main')).toHaveAttribute('data-view-mode', 'sheet');

  // 1. Phone to Telegram Converter (data-sheet="tool-phones")
  await page.locator('.nav a[data-sheet="tool-phones"]').click();
  await expect(page.locator('#tool-phones')).toBeVisible();
  await page.locator('#tool-phones-sample-btn').click();
  await expect(page.locator('#tool-phones-input')).toHaveValue(/\+79991112233/);
  await page.locator('#tool-phones-submit-btn').click();
  await expect(page.locator('#tool-phones-count-badge')).toContainText('4 найдено');
  await expect(page.locator('#tool-phones-results .tool-result-item')).toHaveCount(4);
  await expect(page.locator('#tool-phones-results')).toContainText('В Telegram');

  // 2. Group Parser (data-sheet="tool-parser")
  await page.locator('.nav a[data-sheet="tool-parser"]').click();
  await expect(page.locator('#tool-parser')).toBeVisible();
  await page.locator('.parser-preset-chip').first().click();
  await expect(page.locator('#tool-parser-link')).toHaveValue('https://t.me/saas_founders_hub');
  await page.locator('#tool-parser-submit-btn').click();
  await expect(page.locator('#tool-parser-count-badge')).toContainText('15 участников');
  await expect(page.locator('#tool-parser-results .tool-result-item').first()).toBeVisible();
});

test('F21-H4 Multi-session Inbox switcher and direct message sending across sessions', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#board .card').first()).toBeVisible();

  await page.locator('#toggle-view-mode').click();
  await page.locator('.nav a[data-sheet="inbox"]').click();
  await expect(page.locator('#s04')).toBeVisible();

  const switcher = page.locator('#inbox-session-switcher');
  await expect(switcher).toBeVisible();
  const pills = switcher.locator('.inbox-session-pill');
  await expect(pills).toHaveCount(4); // "Все сессии" + 3 connected demo accounts

  // Switch to a specific Telegram session and back to All sessions
  await pills.nth(1).click();
  await expect(pills.nth(1)).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#inbox-active-session-badge')).toContainText('Сессия:');

  await pills.first().click();
  await expect(pills.first()).toHaveAttribute('aria-pressed', 'true');

  // Select a dialog and send a reply directly from the platform
  const firstDialog = page.locator('#dialog-results .dialog').first();
  await expect(firstDialog).toBeVisible();
  await firstDialog.click();

  const thread = page.locator('#s05');
  await thread.locator('#reply-text').fill('Привет! Отправляю ответ прямо из мульти-сессионного окна Foldesk.');
  await thread.getByRole('button', { name: 'Отправить' }).click();
  await expect(thread.locator('#messages')).toContainText('Привет! Отправляю ответ прямо из мульти-сессионного окна Foldesk.');
});
