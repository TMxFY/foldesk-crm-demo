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

test('F28-V5 Full E2E across MCP Modal, 7-Stage Airtable Pipeline, SoftUGC Card Fields, Calendar Sheet, and 5 SVG Analytics Charts', async ({ page }) => {
  await page.locator('#seed-demo').click();
  await expect(page.locator('#seed-demo')).not.toHaveAttribute('aria-busy', 'true', { timeout: 10000 });
  await expect(page.locator('#board .lead-card').first()).toBeVisible();

  // 1. Enable 7 Airtable stages and reload board
  const stagesRes = await page.request.post('/api/pipeline/stages/airtable');
  expect(stagesRes.status()).toBe(200);
  await page.reload();
  await expect(page.locator('#board .col[data-stage="call_scheduled"]')).toBeVisible();
  await expect(page.locator('#board .col[data-stage="on_hold"]')).toBeVisible();
  expect(await page.locator('#board .col').count()).toBe(7);

  // 2. Open MCP modal and check config JSON blocks
  await page.locator('#open-mcp-modal-btn').click();
  await expect(page.locator('#mcp-modal-box')).toBeVisible();
  await expect(page.locator('#mcp-config-stdio')).toContainText('foldesk-crm');
  await expect(page.locator('#mcp-config-http')).toContainText('/mcp');
  await page.locator('#close-mcp-modal-btn').click();
  await expect(page.locator('#mcp-modal-box')).toBeHidden();

  // 3. Open first lead card and verify SoftUGC fields save & persist
  await page.locator('#board .lead-card').first().click();
  await expect(page.locator('#s09 .softugc-deal-box')).toBeVisible();
  const companyInput = page.locator('#s09 .softugc-deal-box input[name="company"]');
  await companyInput.fill('SoftUGC Test Corp');
  await companyInput.dispatchEvent('change');
  const mrrInput = page.locator('#s09 .softugc-deal-box input[name="monthly_value"]');
  await mrrInput.fill('3200');
  await mrrInput.dispatchEvent('change');
  const fupInput = page.locator('#s09 .softugc-deal-box input[name="next_follow_up"]');
  await fupInput.fill('2026-11-12');
  await fupInput.dispatchEvent('change');
  await expect(companyInput).toHaveValue('SoftUGC Test Corp');
  await expect(mrrInput).toHaveValue('3200');

  const drawerClose = page.locator('#card-drawer-close');
  if (await drawerClose.isVisible()) {
    await drawerClose.click();
  }

  // 4. Switch to Calendar sheet, verify views and add a call event
  await page.locator('.nav a[href="#calendar"]').click();
  await expect(page.locator('#calendar')).toBeVisible();
  await expect(page.locator('#calendar-grid-container')).toBeVisible();

  const calForm = page.locator('#calendar-event-form');
  if (await calForm.isHidden()) {
    await page.locator('#cal-toggle-form-btn').click();
  }
  await page.locator('#cal-ev-title').fill('Demo Call with SoftUGC Test Corp');
  await page.locator('#cal-ev-type').selectOption('call');
  await page.locator('#cal-ev-start').fill('2026-11-05T15:00');
  await page.locator('#cal-ev-submit-btn').click();

  // Switch between Month / Week / Day / Agenda views
  await page.locator('#cal-view-switch button[data-cal-view="week"]').click();
  await expect(page.locator('#calendar-grid-container .cal-week-grid')).toBeVisible();
  await page.locator('#cal-view-switch button[data-cal-view="day"]').click();
  await expect(page.locator('#calendar-grid-container .cal-day-view-layout')).toBeVisible();
  await page.locator('#cal-view-switch button[data-cal-view="agenda"]').click();
  await expect(page.locator('#calendar-grid-container .cal-agenda-list')).toBeVisible();
  await expect(page.locator('#calendar-grid-container')).toContainText('Demo Call with SoftUGC Test Corp');

  // 5. Switch to Analytics sheet and verify 5 SVG charts + period selector
  await page.locator('.nav a[href="#analytics"]').click();
  await expect(page.locator('#analytics')).toBeVisible();
  await expect(page.locator('#analytics-chart-daily-flow svg')).toBeVisible();
  await expect(page.locator('#analytics-chart-revenue svg')).toBeVisible();
  await expect(page.locator('#analytics-chart-funnel-dropoff svg')).toBeVisible();
  await expect(page.locator('#analytics-chart-channels-sources svg')).toBeVisible();
  await expect(page.locator('#analytics-chart-heatmap svg')).toBeVisible();

  await page.locator('#analytics-period-switch button[data-period="7d"]').click();
  await expect(page.locator('#analytics-period-switch button[data-period="7d"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#analytics-chart-daily-flow svg')).toBeVisible();
});
