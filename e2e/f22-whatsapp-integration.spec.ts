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

test('F22-H1 WhatsApp account connection via 8-digit Pairing Code and QR', async ({ page }) => {
  await page.locator('#toggle-view-mode').click();
  await expect(page.locator('#workspace-main')).toHaveAttribute('data-view-mode', 'sheet');

  await page.locator('.nav a[data-sheet="accounts"]').click();
  await expect(page.locator('#accounts')).toBeVisible();

  await page.locator('#open-login-popup').click();
  await expect(page.locator('#account-auth-box')).toBeVisible();

  await page.locator('#auth-tab-btn-whatsapp').click();
  await expect(page.locator('#auth-pane-whatsapp')).toBeVisible();

  await page.locator('#wa-phone-input').fill('+79995554433');
  await page.locator('#wa-code-btn').click();

  await expect(page.locator('#wa-pairing-box')).toBeVisible();
  const pairingCode = page.locator('#wa-pairing-code-display');
  await expect(pairingCode).toBeVisible();
  await expect(pairingCode).toHaveText(/^WA[0-9A-Z]{2}-[0-9A-Z]{4}$/);

  const qrImg = page.locator('#wa-qr-image');
  await expect(qrImg).toBeVisible();
  await expect(qrImg).toHaveAttribute('src', /^data:image\/svg\+xml/);

  await page.locator('#wa-confirm-btn').click();
  await expect(page.locator('#toast')).toContainText('Аккаунт WhatsApp подключён');

  const accountList = page.locator('#account-list');
  await expect(accountList.locator('.channel-badge.channel-wa')).toContainText('WA');
  await expect(accountList).toContainText('+79995554433');
  await expect(accountList).toContainText('Подключён · WhatsApp Web активен');
});

test('F22-H2 Demo seed displays WhatsApp in Kanban with channel filter, Unified Inbox, and Chat', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#board .card').first()).toBeVisible();

  // Check Kanban has both WA and TG badges initially
  await expect(page.locator('#board .card .channel-wa').first()).toBeVisible();
  await expect(page.locator('#board .card .channel-tg').first()).toBeVisible();

  // Filter by WhatsApp only
  await page.locator('#channel-filter').selectOption('whatsapp');
  await expect(page.locator('#board .card')).toHaveCount(3);
  await expect(page.locator('#board .card .channel-wa')).toHaveCount(3);
  await expect(page.locator('#board .card .channel-tg')).toHaveCount(0);

  // Filter by Telegram only
  await page.locator('#channel-filter').selectOption('telegram');
  await expect(page.locator('#board .card')).toHaveCount(8);
  await expect(page.locator('#board .card .channel-tg')).toHaveCount(8);
  await expect(page.locator('#board .card .channel-wa')).toHaveCount(0);

  // Reset channel filter
  await page.locator('#channel-filter').selectOption('');
  await expect(page.locator('#board .card')).toHaveCount(11);

  // Navigate to Unified Inbox (#s04) and open a WhatsApp dialog
  await page.locator('#toggle-view-mode').click();
  await page.locator('.nav a[data-sheet="inbox"]').click();
  await expect(page.locator('#s04')).toBeVisible();

  const waDialogs = page.locator('#dialog-results .dialog').filter({
    has: page.locator('.channel-badge.channel-wa'),
  });
  await expect(waDialogs).toHaveCount(3);

  await waDialogs.first().click();
  const threadHead = page.locator('#thread-head');
  await expect(threadHead.locator('.channel-badge.channel-wa')).toHaveText('WA');
  await expect(threadHead).toContainText('Кирилл Сафонов · BoxBerry Logistics');

  const replyMsg = 'Кирилл, отправляю коммерческое предложение и расчёт на 12 номеров прямо в WhatsApp!';
  await page.locator('#reply-text').fill(replyMsg);
  await page.locator('#reply-form button[type="submit"]').click();
  await expect(page.locator('#messages')).toContainText(replyMsg);
});

test('F22-H3 WhatsApp CSV outreach campaign sends messages to phone recipients', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#board .card').first()).toBeVisible();

  // Find the WhatsApp account option in #campaign-account
  const campAccSelect = page.locator('#campaign-account');
  const waOptionValue = await campAccSelect.locator('option').evaluateAll((opts) => {
    const found = opts.find((o) => (o.textContent || '').includes('[WA]'));
    return found ? (found as HTMLOptionElement).value : '';
  });
  expect(waOptionValue).toBeTruthy();

  // Uncheck other accounts in pool and keep only the WhatsApp account
  const poolCheckboxes = page.locator('#campaign-accounts-pool input[type="checkbox"][data-account-id]');
  const count = await poolCheckboxes.count();
  for (let i = 0; i < count; i += 1) {
    const box = poolCheckboxes.nth(i);
    const id = await box.getAttribute('data-account-id');
    if (id === waOptionValue) {
      await box.check();
    } else {
      await box.uncheck();
    }
  }
  await campAccSelect.selectOption(waOptionValue);

  await page.locator('#campaign-name').fill('WhatsApp Логистика · Холодный тест');
  await page.locator('#create-campaign-btn').click();
  await expect(page.locator('#campaign-list')).toContainText('WhatsApp Логистика · Холодный тест');

  const csvText = [
    'name,wa_phone,company,note',
    'Дмитрий Логист,+79997771122,TransLine WA,Тест доставки',
    'Светлана Мед,+79997773344,ClinicPlus WA,VIP клиника',
  ].join('\n');
  await page.locator('#csv-text').fill(csvText);
  await page.locator('#import-csv-btn').click();

  const waCampaignCard = page.locator('#campaign-list .campaign-card').filter({
    hasText: 'WhatsApp Логистика · Холодный тест',
  });
  await expect(waCampaignCard).toContainText('Дмитрий Логист');
  await expect(waCampaignCard).toContainText('Светлана Мед');

  await waCampaignCard.getByRole('button', { name: 'Запустить шаг рассылки' }).click();
  await expect(page.locator('#toast')).toContainText('Отправлено сообщений: 2');
  await expect(waCampaignCard).toContainText('Отправлено');
});
