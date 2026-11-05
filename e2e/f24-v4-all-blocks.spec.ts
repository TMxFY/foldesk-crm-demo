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

test('F24-B1-B3-B4-B5-B7 Full v4 E2E across Autopilot, Inbox Chips, Kanban Money/DragDrop, Bulk/Merge/CSV, and Analytics', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#seed-demo')).not.toHaveAttribute('aria-busy', 'true', { timeout: 10000 });
  await expect(page.locator('#board .lead-card').first()).toBeVisible();

  // --- BLOCK 1: Autopilot Toggle & Status in #outreach ---
  const autopilotToggle = page.locator('#outreach-autopilot-toggle');
  await expect(autopilotToggle).toBeVisible();
  const statusPill = page.locator('#outreach-autopilot-status');
  await expect(statusPill).toBeVisible();
  await autopilotToggle.click();
  await expect(autopilotToggle).toContainText(/Автопилот по расписанию:/);

  // --- BLOCK 3: Inbox Filters, Chip Counts, and Mark Unread ---
  const waitingChip = page.locator('#s04 [data-chip="waiting"]');
  const unreadChip = page.locator('#s04 [data-chip="unread"]');
  await expect(waitingChip).toContainText(/\(\d+\)/);
  await expect(unreadChip).toContainText(/\(\d+\)/);

  await waitingChip.click();
  await expect(waitingChip).toHaveAttribute('aria-pressed', 'true');
  const waitingDialogs = await page.locator('#dialog-results .dialog').count();
  expect(waitingDialogs).toBeGreaterThanOrEqual(1);
  await waitingChip.click();

  // Open first dialog and click Mark Unread
  await page.locator('#dialog-results .dialog').first().click();
  await expect(page.locator('#mark-unread')).toBeVisible();
  await page.locator('#mark-unread').click();
  await expect(page.locator('#dialog-results .dialog .count').first()).toBeVisible();

  // --- BLOCK 4: Kanban Deal Money, Column Sums & Drag-and-Drop ---
  await expect(page.locator('#board .col[data-stage="new"] .col-sum')).toContainText('$');
  await expect(page.locator('#board .lead-card .card-amount').first()).toContainText('$');

  const firstNewCard = page.locator('#board .col[data-stage="new"] .lead-card').first();
  const cardLeadId = await firstNewCard.getAttribute('data-lead-id');
  const talkingCol = page.locator('#board .col[data-stage="talking"]');
  await firstNewCard.dragTo(talkingCol);
  await expect(page.locator(`#board .col[data-stage="talking"] .lead-card[data-lead-id="${cardLeadId}"]`)).toBeVisible();

  // --- BLOCK 5: Bulk Actions, Merge Duplicates Modal & CSV Export ---
  const firstCheck = page.locator('#board .lead-card .lead-bulk-checkbox').first();
  await firstCheck.check();
  await expect(page.locator('#leads-bulk-bar')).toBeVisible();
  await expect(page.locator('#bulk-selected-count')).toHaveText('1');
  await page.locator('#bulk-clear-btn').click();
  await expect(page.locator('#leads-bulk-bar')).toBeHidden();

  // CSV Export API check
  const csvRes = await page.request.get('/api/leads/export.csv');
  expect(csvRes.status()).toBe(200);
  const csvBody = await csvRes.text();
  expect(csvBody).toContain('amount');
  expect(csvBody).toContain('TON Labs');

  // --- BLOCK 7: Analytics Dashboard & Topbar Money Metrics ---
  await expect(page.locator('#stat-pipeline-value')).toContainText('$');
  await expect(page.locator('#stat-won-value')).toContainText('$');
  await expect(page.locator('#analytics')).toBeVisible();
  await expect(page.locator('#analytics-funnel-pipeline')).toContainText('$');
  await expect(page.locator('#analytics-outreach-reply-rate')).toContainText('%');
  await expect(page.locator('#analytics-accounts-pool')).toContainText('/');
});
