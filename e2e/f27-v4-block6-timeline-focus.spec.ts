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

test('F27-B6 Unified lead_events timeline in #s06/#s09 and focus/today API flow in 4-tab UI', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#seed-demo')).not.toHaveAttribute('aria-busy', 'true', { timeout: 10000 });
  await expect(page.locator('#dialog-results .dialog').first()).toBeVisible();

  // 1. Select dialog for 'Алексей Соколов · TON Labs', open Deal Popup (#s06), and verify compact event timeline
  const alexDialog = page.locator('#dialog-results .dialog').filter({ hasText: 'Алексей Соколов' }).first();
  await alexDialog.click();
  await page.locator('#open-deal-popup-btn').click();
  const sideTimeline = page.locator('#side-card .lead-events-section .event-timeline');
  await expect(sideTimeline).toBeVisible();
  const sideItemsCount = await sideTimeline.locator('.event-timeline-item').count();
  expect(sideItemsCount).toBeGreaterThanOrEqual(2);
  await page.keyboard.press('Escape');

  // 2. Verify /api/focus/today lists waiting replies, due reminders/tasks, and stale leads, and completing reduces counts
  const focusResBefore = await page.request.get('/api/focus/today');
  expect(focusResBefore.ok()).toBeTruthy();
  const focusBefore = await focusResBefore.json();
  expect((focusBefore.waiting_reply || []).length).toBeGreaterThanOrEqual(3);
  const dueReminders = (focusBefore.due_reminders || []).filter((x: { kind?: string }) => x.kind !== 'task');
  const openTasks = (focusBefore.open_tasks || []).length
    ? focusBefore.open_tasks
    : (focusBefore.due_reminders || []).filter((x: { kind?: string }) => x.kind === 'task');
  expect(dueReminders.length + openTasks.length).toBeGreaterThanOrEqual(3);
  expect((focusBefore.stale_leads || []).length).toBeGreaterThanOrEqual(2);

  const remDoneRes = await page.request.post(`/api/reminders/${dueReminders[0].id}/done`);
  expect(remDoneRes.ok()).toBeTruthy();
  const taskDoneRes = await page.request.post(`/api/tasks/${openTasks[0].id}/done`, {
    data: { done: true },
  });
  expect(taskDoneRes.ok()).toBeTruthy();

  const focusResAfter = await page.request.get('/api/focus/today');
  expect(focusResAfter.ok()).toBeTruthy();
  const focusAfter = await focusResAfter.json();
  const dueAfter = (focusAfter.due_reminders || []).filter((x: { kind?: string }) => x.kind !== 'task').length;
  const tasksAfter = ((focusAfter.open_tasks || []).length
    ? focusAfter.open_tasks
    : (focusAfter.due_reminders || []).filter((x: { kind?: string }) => x.kind === 'task')).length;
  expect(dueAfter + tasksAfter).toBe(dueReminders.length + openTasks.length - 2);

  // 3. Switch to Лиды tab, open 'Илья Ковалёв · CloudNine' card (#s09), and verify timeline + status_changed event
  await page.locator('.nav a[data-sheet-link="leads"]').click();
  const ilyaCard = page.locator('#board .lead-card').filter({ hasText: 'Илья Ковалёв' }).first();
  await expect(ilyaCard).toBeVisible();
  await ilyaCard.click();

  const cardName = page.locator('#lead-card #card-name');
  await expect(cardName).toBeVisible();
  await expect(cardName).toContainText('Илья Ковалёв');

  const cardTimeline = page.locator('#lead-card .lead-events-section .event-timeline');
  await expect(cardTimeline).toBeVisible();
  const initialCardEventCount = await cardTimeline.locator('.event-timeline-item').count();
  expect(initialCardEventCount).toBeGreaterThanOrEqual(3);

  // 4. Change status on #lead-card and verify new status_changed event appears in #s09 timeline
  const cardStatusSelect = page.locator('#lead-card select[aria-label="Статус"]');
  await cardStatusSelect.selectOption('won');

  await expect(page.locator('#lead-card #card-name')).toContainText('Илья Ковалёв');
  await expect(cardTimeline.locator('.event-timeline-item[data-event-type="status_changed"]').first()).toContainText('won');
  const updatedCardEventCount = await cardTimeline.locator('.event-timeline-item').count();
  expect(updatedCardEventCount).toBeGreaterThan(initialCardEventCount);
});
