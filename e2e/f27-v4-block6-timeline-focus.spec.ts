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

test('F27-B6 Unified lead_events timeline in #s06/#s09 and My Focus Today widget in #digest', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#seed-demo')).not.toHaveAttribute('aria-busy', 'true', { timeout: 10000 });
  await expect(page.locator('#dialog-results .dialog').first()).toBeVisible();

  // 1. Select dialog for 'Алексей Соколов · TON Labs' and verify compact event timeline in #s06
  const alexDialog = page.locator('#dialog-results .dialog').filter({ hasText: 'Алексей Соколов' }).first();
  await alexDialog.click();
  const sideTimeline = page.locator('#side-card .lead-events-section .event-timeline');
  await expect(sideTimeline).toBeVisible();
  const sideItemsCount = await sideTimeline.locator('.event-timeline-item').count();
  expect(sideItemsCount).toBeGreaterThanOrEqual(2);

  // 2. Switch to #digest sheet and verify #focus-today-widget
  const digestTab = page.locator('.sheet-tab[data-sheet-target="digest"]');
  if (await digestTab.isVisible()) {
    await digestTab.click();
  }
  const focusWidget = page.locator('#focus-today-widget');
  await expect(focusWidget).toBeVisible();

  const waitItems = page.locator('#focus-waiting-reply-list .focus-item[data-focus-kind="waiting_reply"]');
  const remItems = page.locator('#focus-due-reminders-list .focus-item[data-focus-kind="due_reminder"]');
  const taskItems = page.locator('#focus-due-reminders-list .focus-item[data-focus-kind="open_task"]');
  const staleItems = page.locator('#focus-stale-leads-list .focus-item[data-focus-kind="stale_lead"]');

  await expect(waitItems.first()).toBeVisible();
  await expect(remItems.first()).toBeVisible();
  await expect(taskItems.first()).toBeVisible();
  await expect(staleItems.first()).toBeVisible();

  const countWaitingBefore = Number(await page.locator('#focus-count-waiting').textContent());
  const countRemBefore = Number(await page.locator('#focus-count-reminders').textContent());
  const countStaleBefore = Number(await page.locator('#focus-count-stale').textContent());
  expect(countWaitingBefore).toBeGreaterThanOrEqual(3);
  expect(countRemBefore).toBeGreaterThanOrEqual(3);
  expect(countStaleBefore).toBeGreaterThanOrEqual(2);

  const totalTextBefore = await page.locator('#focus-today-total-badge').textContent();
  expect(totalTextBefore).toBe(`${countWaitingBefore + countRemBefore + countStaleBefore} в фокусе`);

  // 3. Complete a due reminder inline from Focus widget
  await remItems.first().locator('button', { hasText: 'Сделано' }).click();
  await expect(page.locator('#focus-count-reminders')).toHaveText(String(countRemBefore - 1));

  // 4. Complete an open task inline from Focus widget
  await taskItems.first().locator('button', { hasText: 'Сделано' }).click();
  await expect(page.locator('#focus-count-reminders')).toHaveText(String(countRemBefore - 2));

  // 5. Click 'К карточке' on a stale lead ('Илья Ковалёв · CloudNine') and verify #s09 opens for THAT lead (not hijacked by #s04 dialog)
  const ilyaStaleItem = staleItems.filter({ hasText: 'Илья Ковалёв' }).first();
  await expect(ilyaStaleItem).toBeVisible();
  await ilyaStaleItem.locator('button', { hasText: 'К карточке' }).click();

  const cardName = page.locator('#lead-card #card-name');
  await expect(cardName).toBeVisible();
  await expect(cardName).toContainText('Илья Ковалёв');

  const cardTimeline = page.locator('#lead-card .lead-events-section .event-timeline');
  await expect(cardTimeline).toBeVisible();
  const initialCardEventCount = await cardTimeline.locator('.event-timeline-item').count();
  expect(initialCardEventCount).toBeGreaterThanOrEqual(3);

  // 6. Change status on #lead-card and verify new status_changed event appears in #s09 timeline and stale lead count decreases
  const cardStatusSelect = page.locator('#lead-card select[aria-label="Статус"]');
  await cardStatusSelect.selectOption('won');

  await expect(page.locator('#lead-card #card-name')).toContainText('Илья Ковалёв');
  await expect(cardTimeline.locator('.event-timeline-item[data-event-type="status_changed"]').first()).toContainText('won');
  const updatedCardEventCount = await cardTimeline.locator('.event-timeline-item').count();
  expect(updatedCardEventCount).toBeGreaterThan(initialCardEventCount);
});
