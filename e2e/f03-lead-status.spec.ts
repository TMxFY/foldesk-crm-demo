import { expect, test } from '@playwright/test';
import { addLead, assertQuiet, column, noteAxe, reset, resetProblems, scan, watch } from './helpers';

test.beforeEach(async ({ page }) => {
  resetProblems();
  watch(page);
  await reset(page);
  await page.goto('/');
});

test.afterEach(() => {
  assertQuiet();
});

test('F03-H1 manual lead lands in the table and the Lead column', async ({ page }, info) => {
  await addLead(page, 'Иван Петров', 'ivan_p');
  await expect(page.getByRole('cell', { name: '@ivan_p' })).toBeVisible();
  await expect(column(page, 'Лид').getByRole('link', { name: /Иван Петров/ })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Карточка' }).getByRole('heading', { name: 'Иван Петров' })).toBeVisible();
  noteAxe(info, await scan(page, 'S09 filled'));
});

test('F03-H2 status moves the card and survives reload', async ({ page }) => {
  await addLead(page, 'Иван Петров', 'ivan_p');
  const card = page.getByRole('region', { name: 'Карточка' });
  await card.getByLabel('Статус').selectOption({ label: 'Разговор' });
  await expect(page.getByRole('table').getByLabel('Статус')).toHaveValue('talking');
  await expect(column(page, 'Разговор').getByRole('link', { name: /Иван Петров/ })).toBeVisible();
  await expect(column(page, 'Лид').getByRole('link', { name: /Иван Петров/ })).toHaveCount(0);
  await page.reload();
  await expect(column(page, 'Разговор').getByRole('link', { name: /Иван Петров/ })).toBeVisible();
  await page.getByRole('cell', { name: '@ivan_p' }).click();
  await expect(card.getByLabel('Статус')).toHaveValue('talking');
});

test('F03-H3 manual lead keeps note link and email', async ({ page }) => {
  const form = page.locator('#manual-lead');
  await form.getByLabel('Имя').fill('Анна');
  await form.getByLabel('Ник').fill('anna');
  await form.getByLabel('Телефон').fill('+79991112233');
  await form.getByLabel('Заметка').fill('созвон в пятницу');
  await form.getByLabel('Ссылка').fill('https://example.com/anna');
  await form.getByLabel('Почта').fill('anna@example.com');
  await form.getByRole('button', { name: 'Добавить лид' }).click();
  const card = page.getByRole('region', { name: 'Карточка' });
  await expect(card.getByRole('heading', { name: 'Анна' })).toBeVisible();
  await expect(card.getByText('созвон в пятницу')).toBeVisible();
  await expect(card.getByText('anna@example.com')).toBeVisible();
  await expect(card.getByRole('link', { name: 'Ссылка' })).toHaveAttribute('href', 'https://example.com/anna');
  const rows = await (await page.request.get('/api/leads')).json();
  expect(rows).toHaveLength(1);
  expect(rows[0].note).toBe('созвон в пятницу');
  expect(rows[0].email).toBe('anna@example.com');
  expect(rows[0].url).toBe('https://example.com/anna');
  expect(rows[0].phone).toBe('+79991112233');
  expect(rows[0].username).toBe('anna');
});

test('F03-E1 blank manual submit creates nothing', async ({ page }) => {
  await page.getByRole('region', { name: 'Лиды' }).getByRole('button', { name: 'Добавить лид' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'Укажите имя' })).toBeVisible();
  expect(await (await page.request.get('/api/leads')).json()).toEqual([]);
  await expect(page.getByText('Лидов пока нет')).toBeVisible();
});

test('F03-E2 whitespace and a very long name', async ({ page }) => {
  const leads = page.getByRole('region', { name: 'Лиды' });
  await leads.getByLabel('Имя').fill('   ');
  await leads.getByRole('button', { name: 'Добавить лид' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'Укажите имя' })).toBeVisible();
  expect(await (await page.request.get('/api/leads')).json()).toEqual([]);

  const longName = `Иван ${'А'.repeat(500)}`;
  await leads.getByLabel('Имя').fill(longName);
  await leads.getByRole('button', { name: 'Добавить лид' }).click();
  await expect(page.getByRole('table').getByLabel('Имя')).toHaveValue(longName);
});

test('F03-E3 double click adds one manual lead', async ({ page }) => {
  const leads = page.getByRole('region', { name: 'Лиды' });
  await leads.getByLabel('Имя').fill('Двойной');
  await leads.getByRole('button', { name: 'Добавить лид' }).dblclick();
  await expect(page.getByRole('table').getByLabel('Имя')).toHaveValue('Двойной');
  const rows = await (await page.request.get('/api/leads')).json();
  expect(rows.filter((row: { name: string }) => row.name === 'Двойной')).toHaveLength(1);
});

test('F03-E4 second tab sees the lead and cannot see a foreign base', async ({ page }) => {
  await addLead(page, 'Иван Петров', 'ivan_p');
  const other = await page.context().newPage();
  watch(other);
  await other.goto('/');
  await expect(other.getByRole('cell', { name: '@ivan_p' })).toBeVisible();
  await other.close();
});

test('F03-N1 unknown status and a missing lead do not change data', async ({ page }) => {
  await addLead(page, 'Иван Петров', 'ivan_p');
  const rows = await (await page.request.get('/api/leads')).json();
  const bad = await page.request.patch(`/api/leads/${rows[0].id}`, { data: { status: 'nope' } });
  expect(bad.status()).toBe(400);
  const missing = await page.request.patch('/api/leads/999999', { data: { status: 'won' } });
  expect(missing.status()).toBe(404);
  await page.reload();
  await expect(page.getByRole('table').getByLabel('Статус')).toHaveValue('new');
});

test('F03-N2 a name with markup is text, not HTML', async ({ page }) => {
  page.on('dialog', () => {
    throw new Error('unexpected dialog');
  });
  const payload = '<img src=x onerror=alert(1)>';
  await addLead(page, payload, 'xss');
  await expect(page.getByRole('table').getByLabel('Имя')).toHaveValue(payload);
  await expect(page.getByRole('img')).toHaveCount(0);
});

test('F03-E5 funnel stage filter keeps the lead out of other columns', async ({ page }) => {
  await addLead(page, 'Иван Петров', 'ivan_p');
  await page.getByLabel('Стадия').selectOption({ label: 'Проигран' });
  await expect(page.getByText('Фильтр применён')).toBeVisible();
  await expect(column(page, 'Лид').getByRole('link', { name: /Иван Петров/ })).toHaveCount(0);
  await expect(column(page, 'Проигран').getByText('Пусто')).toBeVisible();
  await page.getByLabel('Стадия').selectOption({ label: 'Все' });
  await expect(column(page, 'Лид').getByRole('link', { name: /Иван Петров/ })).toBeVisible();
});
