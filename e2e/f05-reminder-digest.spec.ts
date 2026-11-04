import { expect, test } from '@playwright/test';
import { addLead, assertQuiet, noteAxe, reset, resetProblems, scan, watch } from './helpers';

test.beforeEach(async ({ page }) => {
  resetProblems();
  watch(page);
  await reset(page);
  await page.goto('/');
  await addLead(page, 'Иван Петров', 'ivan_p');
});

test.afterEach(() => {
  assertQuiet();
});

test('F05-H1 reminder keeps the clock time on the card and in the digest', async ({ page }, info) => {
  const card = page.getByRole('region', { name: 'Карточка' });
  await card.getByRole('button', { name: 'Напоминание' }).click();
  const dialog = page.getByRole('dialog', { name: 'Напоминание' });
  noteAxe(info, await scan(page, 'S13 reminder dialog'));
  await dialog.getByLabel('Дата').fill('2026-12-15');
  await dialog.getByLabel('Время').fill('09:30');
  await dialog.getByLabel('Текст').fill('созвон');
  await dialog.getByRole('button', { name: 'Записать' }).click();
  await expect(dialog).toBeHidden();
  await expect(card.getByText('созвон')).toBeVisible();
  await expect(card.getByText('09:30')).toBeVisible();
  const digest = page.getByRole('region', { name: 'Напоминания' });
  await expect(digest.getByRole('heading', { name: 'созвон' })).toBeVisible();
  await expect(digest.getByText('09:30')).toBeVisible();
  await page.reload();
  await expect(digest.getByRole('heading', { name: 'созвон' })).toBeVisible();
  await expect(digest.getByText('09:30')).toBeVisible();
});

test('F05-H2 digest overdue, open the lead, then complete', async ({ page }) => {
  const lead = (await (await page.request.get('/api/leads')).json())[0];
  const created = await page.request.post('/api/reminders', {
    data: { lead_id: lead.id, due_at: '2000-01-01T00:00:00', text: 'позвонить' },
  });
  expect(created.ok()).toBeTruthy();
  await page.reload();
  const digest = page.getByRole('region', { name: 'Напоминания' });
  await digest.getByRole('button', { name: 'Просроченные' }).click();
  await expect(digest.getByRole('heading', { name: 'Просрочено' })).toBeVisible();
  await expect(digest.getByRole('heading', { name: 'позвонить' })).toBeVisible();
  await digest.getByRole('link', { name: /К лиду: позвонить/ }).click();
  await expect(page.getByRole('region', { name: 'Карточка' }).getByRole('heading', { name: 'Иван Петров' })).toBeVisible();
  await digest.getByRole('button', { name: /Сделано: позвонить/ }).click();
  await expect(digest.getByRole('heading', { name: 'позвонить' })).toHaveCount(0);
  await digest.getByRole('button', { name: 'Все' }).click();
  await expect(digest.getByRole('article').filter({ hasText: 'позвонить' }).getByText('Сделано')).toBeVisible();
  const open = await (await page.request.get('/api/reminders?scope=open')).json();
  expect(open).toEqual([]);
});

test('F05-E1 empty reminder does not save', async ({ page }) => {
  await page.getByRole('region', { name: 'Карточка' }).getByRole('button', { name: 'Напоминание' }).click();
  const dialog = page.getByRole('dialog', { name: 'Напоминание' });
  await dialog.getByRole('button', { name: 'Записать' }).click();
  await expect(dialog).toBeVisible();
  const dateMissing = await dialog.getByLabel('Дата').evaluate((el: HTMLInputElement) => el.validity.valueMissing);
  expect(dateMissing).toBe(true);
  expect(await (await page.request.get('/api/reminders')).json()).toEqual([]);
});

test('F05-E2 postpone saves the new due time', async ({ page }) => {
  const lead = (await (await page.request.get('/api/leads')).json())[0];
  const created = await page.request.post('/api/reminders', {
    data: { lead_id: lead.id, due_at: '2000-01-01T00:00:00', text: 'позвонить' },
  });
  expect(created.ok()).toBeTruthy();
  const before = (await (await page.request.get('/api/reminders?scope=all')).json())[0].due_at;
  await page.reload();
  const digest = page.getByRole('region', { name: 'Напоминания' });
  const item = digest.getByRole('article').filter({ hasText: 'позвонить' });
  await item.getByRole('button', { name: /Отложить/ }).click();
  await item.getByLabel('Дата').fill('2026-12-02');
  await item.getByLabel('Время').fill('10:00');
  await item.getByRole('button', { name: 'Сохранить срок' }).click();
  await expect(digest.getByRole('article').filter({ hasText: 'позвонить' }).getByText('10:00')).toBeVisible();
  const after = (await (await page.request.get('/api/reminders?scope=all')).json())[0];
  expect(after.due_at).not.toBe(before);
  expect(after.done_at).toBeNull();
  expect(after.text).toBe('позвонить');
});

test('F05-E3 double click creates one reminder', async ({ page }) => {
  const form = page.getByRole('row').filter({ hasText: '@ivan_p' });
  await form.getByLabel('Дата').fill('2026-12-16');
  await form.getByLabel('Время').fill('11:00');
  await form.getByLabel('Текст напоминания').fill('дважды');
  await form.getByRole('button', { name: 'Поставить' }).dblclick();
  await expect(page.getByRole('region', { name: 'Напоминания' }).getByRole('heading', { name: 'дважды' })).toBeVisible();
  const rows = await (await page.request.get('/api/reminders')).json();
  expect(rows.filter((row: { text: string }) => row.text === 'дважды')).toHaveLength(1);
});

test('F05-E4 mobile digest and keyboard focus on the reminder dialog', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await expect(page.getByRole('heading', { name: 'Напоминания' })).toBeVisible();
  await page.getByRole('region', { name: 'Карточка' }).getByRole('button', { name: 'Напоминание' }).click();
  await expect(page.getByRole('dialog', { name: 'Напоминание' }).getByLabel('Дата')).toBeFocused();
});

test('F05-E5 open the lead from the digest while CRM search hides the row', async ({ page }) => {
  const lead = (await (await page.request.get('/api/leads')).json())[0];
  const created = await page.request.post('/api/reminders', {
    data: { lead_id: lead.id, due_at: '2000-01-01T00:00:00', text: 'позвонить' },
  });
  expect(created.ok()).toBeTruthy();
  await page.reload();
  await page.getByLabel('Запрос').fill('нет-такого');
  await page.getByRole('button', { name: 'Найти' }).click();
  const card = page.getByRole('region', { name: 'Карточка' });
  await expect(card.getByText('Выберите лида в списке или на воронке')).toBeVisible();
  await expect(page.getByRole('cell', { name: '@ivan_p' })).toHaveCount(0);
  const digest = page.getByRole('region', { name: 'Напоминания' });
  await digest.getByRole('link', { name: /К лиду: позвонить/ }).click();
  await expect(card.getByRole('heading', { name: 'Иван Петров' })).toBeVisible();
  await expect(page.getByRole('cell', { name: '@ivan_p' })).toHaveCount(0);
});

test('F05-H3 a card task has no due date', async ({ page }) => {
  const card = page.getByRole('region', { name: 'Карточка' });
  await expect(card.getByLabel('Дата задачи')).toHaveCount(0);
  await expect(card.getByLabel('Время задачи')).toHaveCount(0);
  await card.getByLabel('Текст задачи').fill('написать');
  await card.getByRole('button', { name: 'Поставить задачу' }).click();
  await expect(card.getByRole('checkbox', { name: 'написать' })).toBeVisible();
  const lead = (await (await page.request.get('/api/leads')).json())[0];
  let body = await (await page.request.get(`/api/leads/${lead.id}/card`)).json();
  const open = body.tasks.find((row: { title: string }) => row.title === 'написать');
  expect(open.done_at).toBeNull();
  expect(open.due_at).toBeUndefined();
  expect(await (await page.request.get('/api/reminders?scope=all')).json()).toEqual([]);
  await card.getByRole('checkbox', { name: 'написать' }).check();
  body = await (await page.request.get(`/api/leads/${lead.id}/card`)).json();
  expect(body.tasks.find((row: { title: string }) => row.title === 'написать').done_at).toBeTruthy();
});

test('F05-N1 missing lead cannot take a reminder', async ({ page }) => {
  const res = await page.request.post('/api/reminders', {
    data: { lead_id: 999999, due_at: '2099-01-01T00:00:00', text: 'нет' },
  });
  expect(res.status()).toBe(404);
  expect(await (await page.request.get('/api/reminders')).json()).toEqual([]);
});
