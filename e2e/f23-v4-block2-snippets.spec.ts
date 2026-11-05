import fs from 'fs';
import { expect, test } from '@playwright/test';
import { assertQuiet, reset, resetProblems, watch } from './helpers';

const chromePath = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
if (fs.existsSync(chromePath)) {
  test.use({ launchOptions: { executablePath: chromePath } });
}

test.beforeEach(async ({ page }) => {
  resetProblems();
  watch(page);
  await reset(page);
  await page.goto('/');
});

test.afterEach(() => {
  assertQuiet();
});

test('F23-B2 Quick reply snippets in #s06 and #s05 substitute {name}/{company}, copy to clipboard, and support add/delete', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#dialog-results .dialog').first()).toBeVisible();

  // Select dialog for 'Алексей Соколов · TON Labs'
  const alexDialog = page.locator('#dialog-results .dialog').filter({ hasText: 'Алексей Соколов' }).first();
  await alexDialog.click();
  await expect(page.locator('#reply-form')).toBeVisible();
  await expect(page.locator('#side-snippets-panel')).toBeVisible();

  // 1. Default snippets (/price, /cases, /call) exist in both #s06 and #s05
  for (const sc of ['/price', '/cases', '/call']) {
    await expect(page.locator(`#side-snippets-list .snippet-chip-btn[data-snippet-shortcut="${sc}"]`)).toBeVisible();
    await expect(page.locator(`#reply-snippets-list .snippet-chip-btn[data-snippet-shortcut="${sc}"]`)).toBeVisible();
  }

  // 2. Click /price in #s06 -> substitutes {name} and {company}, copies to clipboard, inserts into #reply-text
  await page.locator('#side-snippets-list .snippet-chip-btn[data-snippet-shortcut="/price"]').click();
  const replyText = page.locator('#reply-text');
  await expect(replyText).toHaveValue(/Алексей Соколов/);
  await expect(replyText).toHaveValue(/TON Labs/);
  const valAfterPrice = await replyText.inputValue();
  expect(valAfterPrice).not.toContain('{name}');
  expect(valAfterPrice).not.toContain('{company}');

  const copied = await page.evaluate(() => (window as unknown as { __lastCopiedSnippet?: string }).__lastCopiedSnippet);
  expect(copied).toBe(valAfterPrice);
  await expect(page.locator('#snippet-copy-status')).toContainText('TON Labs');

  // 3. Slash autocomplete in #reply-text
  await replyText.fill('/ca');
  const suggestBtn = page.locator('#reply-snippets-suggest [data-suggest-shortcut="/cases"]');
  await expect(suggestBtn).toBeVisible();
  await suggestBtn.click();
  await expect(replyText).toHaveValue(/Алексей Соколов/);
  await expect(replyText).toHaveValue(/кейсы под TON Labs/i);

  // 4. Add custom snippet via #snippet-create-form in #s06
  await page.locator('#snippet-shortcut').fill('/audit');
  await page.locator('#snippet-title').fill('Аудит');
  await page.locator('#snippet-body').fill('Привет, {name}! Сделаем аудит для {company} за 24 часа.');
  await page.locator('#snippet-submit-btn').click();

  const auditChipSide = page.locator('#side-snippets-list .snippet-chip-btn[data-snippet-shortcut="/audit"]');
  const auditChipReply = page.locator('#reply-snippets-list .snippet-chip-btn[data-snippet-shortcut="/audit"]');
  await expect(auditChipSide).toBeVisible();
  await expect(auditChipReply).toBeVisible();

  await auditChipReply.click();
  const expectedCustom = 'Привет, Алексей Соколов! Сделаем аудит для TON Labs за 24 часа.';
  await expect(replyText).toHaveValue(expectedCustom);
  const copiedCustom = await page.evaluate(() => (window as unknown as { __lastCopiedSnippet?: string }).__lastCopiedSnippet);
  expect(copiedCustom).toBe(expectedCustom);

  // 5. Delete custom snippet via its delete button
  await page.locator('#side-snippets-list .snippet-delete-btn[data-delete-snippet-shortcut="/audit"]').click();
  await expect(auditChipSide).toHaveCount(0);
  await expect(auditChipReply).toHaveCount(0);

  // 6. Switch session tab to another account (WA session) -> snippet uses that session's conversation (not stale Account #1 dialog)
  const waSessionPill = page.locator('#inbox-session-switcher .inbox-session-pill').filter({ hasText: 'WA' }).first();
  await expect(waSessionPill).toBeVisible();
  await waSessionPill.click();
  await expect(page.locator('#dialog-results .dialog').filter({ hasText: 'Кирилл Сафонов' }).first()).toBeVisible();
  await page.locator('#side-snippets-list .snippet-chip-btn[data-snippet-shortcut="/price"]').click();
  await expect(replyText).toHaveValue(/Кирилл Сафонов/);
  await expect(replyText).toHaveValue(/BoxBerry Logistics/);
  await expect(replyText).not.toHaveValue(/Алексей Соколов/);
  await expect(replyText).not.toHaveValue(/TON Labs/);

  // Switch back to all sessions (Telegram account #1)
  await page.locator('#inbox-session-switcher .inbox-session-pill[data-session-filter="all"]').click();

  // 7. Select an unlinked Telegram dialog ('nolead', e.g. Пётр) -> uses Пётр / вашей компании, never leaks another lead
  await page.locator('#search-mode [data-mode="telegram"]').click();
  await page.locator('#q').fill('Пётр');
  await page.locator('#search-form button[type="submit"]').click();
  const petrDialog = page.locator('#dialog-results .dialog').filter({ hasText: 'Пётр' }).first();
  await expect(petrDialog).toBeVisible();
  await petrDialog.click();
  await expect(page.locator('#side-view')).toHaveAttribute('data-state', 'nolead');

  await page.locator('#side-snippets-list .snippet-chip-btn[data-snippet-shortcut="/price"]').click();
  await expect(replyText).toHaveValue(/Привет, Пётр!/);
  await expect(replyText).toHaveValue(/для вашей компании/);
  await expect(replyText).not.toHaveValue(/Алексей Соколов/);
  await expect(replyText).not.toHaveValue(/Кирилл Сафонов/);
});
