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

test('F23-B2 Quick reply snippets in #s05 substitute {name}/{company}, copy to clipboard, and support add/delete', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#dialog-results .dialog').first()).toBeVisible();

  // Select dialog for 'Алексей Соколов · TON Labs'
  const alexDialog = page.locator('#dialog-results .dialog').filter({ hasText: 'Алексей Соколов' }).first();
  await alexDialog.click();
  await expect(page.locator('#reply-form')).toBeVisible();
  await expect(page.locator('#reply-snippets-bar')).toBeVisible();

  // 1. Default snippets (/price, /cases, /call) exist in #reply-snippets-list (and #side-snippets-list DOM)
  for (const sc of ['/price', '/cases', '/call']) {
    await expect(page.locator(`#reply-snippets-list .snippet-chip-btn[data-snippet-shortcut="${sc}"]`)).toBeVisible();
    await expect(page.locator(`#side-snippets-list .snippet-chip-btn[data-snippet-shortcut="${sc}"]`)).toHaveCount(1);
  }

  // 2. Click /price in #reply-snippets-list -> substitutes {name} and {company}, copies to clipboard, inserts into #reply-text
  await page.locator('#reply-snippets-list .snippet-chip-btn[data-snippet-shortcut="/price"]').click();
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

  // 4. Add custom snippet via POST /api/snippets and verify rendered in #reply-snippets-list
  const addRes = await page.request.post('/api/snippets', {
    data: {
      shortcut: '/audit',
      title: 'Аудит',
      body: 'Привет, {name}! Сделаем аудит для {company} за 24 часа.',
    },
  });
  expect(addRes.ok()).toBeTruthy();
  await page.reload();
  await page.locator('#dialog-results .dialog').filter({ hasText: 'Алексей Соколов' }).first().click();

  const auditChipReply = page.locator('#reply-snippets-list .snippet-chip-btn[data-snippet-shortcut="/audit"]');
  await expect(auditChipReply).toBeVisible();

  await auditChipReply.click();
  const expectedCustom = 'Привет, Алексей Соколов! Сделаем аудит для TON Labs за 24 часа.';
  await expect(page.locator('#reply-text')).toHaveValue(expectedCustom);
  const copiedCustom = await page.evaluate(() => (window as unknown as { __lastCopiedSnippet?: string }).__lastCopiedSnippet);
  expect(copiedCustom).toBe(expectedCustom);

  // 5. Delete custom snippet via its delete button in #reply-snippets-list
  await page.locator('#reply-snippets-list .snippet-delete-btn[data-delete-snippet-shortcut="/audit"]').click();
  await expect(auditChipReply).toHaveCount(0);

  // 6. Switch session tab to another account (WA session) -> snippet uses that session's conversation (not stale Account #1 dialog)
  const waSessionPill = page.locator('#inbox-session-switcher .inbox-session-pill').filter({ hasText: 'WA' }).first();
  await expect(waSessionPill).toBeVisible();
  await waSessionPill.click();
  await expect(page.locator('#dialog-results .dialog').filter({ hasText: 'Кирилл Сафонов' }).first()).toBeVisible();
  await page.locator('#reply-snippets-list .snippet-chip-btn[data-snippet-shortcut="/price"]').click();
  await expect(page.locator('#reply-text')).toHaveValue(/Кирилл Сафонов/);
  await expect(page.locator('#reply-text')).toHaveValue(/BoxBerry Logistics/);
  await expect(page.locator('#reply-text')).not.toHaveValue(/Алексей Соколов/);
  await expect(page.locator('#reply-text')).not.toHaveValue(/TON Labs/);

  // Switch back to all sessions (Telegram account #1)
  await page.locator('#inbox-session-switcher .inbox-session-pill[data-session-filter="all"]').click();

  // 7. Create a company-less CRM contact ('Пётр', @petr), search in Telegram mode, and verify fallback substitution
  const petrLeadRes = await page.request.post('/api/leads', {
    data: { name: 'Пётр', username: 'petr', status: 'talking' },
  });
  expect(petrLeadRes.ok()).toBeTruthy();
  const petrLead = await petrLeadRes.json();
  const petrDlgRes = await page.request.post('/api/dialogs/open-by-lead', {
    data: { lead_id: petrLead.id, username: 'petr', name: 'Пётр' },
  });
  expect(petrDlgRes.ok()).toBeTruthy();
  const petrDlg = await petrDlgRes.json();
  const petrMsgRes = await page.request.post(`/api/dialogs/${petrDlg.id}/messages`, {
    data: { text: 'Добрый день!' },
  });
  expect(petrMsgRes.ok()).toBeTruthy();
  await page.locator('#search-mode [data-mode="telegram"]').click();
  await page.locator('#q').fill('Пётр');
  await page.locator('#search-form button[type="submit"]').click();
  const petrDialog = page.locator('#dialog-results .dialog').filter({ hasText: 'Пётр' }).first();
  await expect(petrDialog).toBeVisible();
  await petrDialog.click();

  await page.locator('#reply-snippets-list .snippet-chip-btn[data-snippet-shortcut="/price"]').click();
  await expect(page.locator('#reply-text')).toHaveValue(/Привет, Пётр!/);
  await expect(page.locator('#reply-text')).toHaveValue(/для вашей компании/);
  await expect(page.locator('#reply-text')).not.toHaveValue(/Алексей Соколов/);
  await expect(page.locator('#reply-text')).not.toHaveValue(/Кирилл Сафонов/);
});
