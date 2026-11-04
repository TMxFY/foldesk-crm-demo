import { expect, test } from '@playwright/test';
import { addLead, assertQuiet, connect, reset, resetProblems, watch } from './helpers';

test.beforeEach(async ({ page }) => {
  resetProblems();
  watch(page);
  await reset(page);
  await page.goto('/');
});

test.afterEach(() => {
  assertQuiet();
});

test('F02-H1 CRM search opens the lead', async ({ page }) => {
  await addLead(page, 'Иван Петров', 'ivan');
  await addLead(page, 'Мария', 'maria');
  await page.getByLabel('Запрос').fill('иван');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.getByRole('table').getByLabel('Имя')).toHaveValue('Иван Петров');
  await expect(page.getByRole('cell', { name: '@maria' })).toHaveCount(0);
  await page.getByRole('cell', { name: '@ivan' }).click();
  await expect(page.getByRole('region', { name: 'Карточка' }).getByRole('heading', { name: 'Иван Петров' })).toBeVisible();
});

test('F02-H2 Telegram search opens a dialog and adds one lead', async ({ page }) => {
  await connect(page);
  await page.getByRole('button', { name: 'В Telegram' }).click();
  await expect(page.getByRole('combobox', { name: /Аккаунт/ })).toBeVisible();
  await page.getByLabel('Запрос').fill('@ivan');
  await page.getByRole('button', { name: 'Найти' }).click();
  await page.getByRole('button', { name: /Иван Петров/ }).click();
  const thread = page.getByRole('region', { name: 'Переписка' });
  await expect(thread.getByRole('heading', { name: 'Иван Петров' })).toBeVisible();
  await expect(thread.getByText(/С аккаунта/)).toBeVisible();
  await page.getByRole('button', { name: 'В лиды' }).dblclick();
  await expect(page.getByRole('region', { name: 'Карточка' }).getByRole('heading', { name: 'Иван Петров' })).toBeVisible();
  const leads = await (await page.request.get('/api/leads')).json();
  expect(leads).toHaveLength(1);

  await thread.getByRole('button', { name: 'Отправить' }).click();
  await expect(thread.getByRole('alert')).toHaveText('Напишите сообщение');
  await expect(thread.getByRole('list')).toBeEmpty();
  await thread.getByLabel('Ответ').fill('один ответ');
  await thread.getByRole('button', { name: 'Отправить' }).click();
  await expect(thread.getByRole('listitem')).toHaveCount(1);
  await expect(thread.getByText('один ответ')).toBeVisible();
  const accounts = await (await page.request.get('/api/accounts')).json();
  const inbox = await (await page.request.get(`/api/inbox?account_id=${accounts[0].id}`)).json();
  const ivan = inbox.find((row: { username: string }) => row.username === 'ivan');
  const messages = await (await page.request.get(`/api/dialogs/${ivan.id}/messages`)).json();
  expect(messages).toHaveLength(1);
  expect(messages[0].text).toBe('один ответ');
});

test('F02-E1 no CRM match hides other leads', async ({ page }) => {
  await addLead(page, 'Мария', 'maria');
  await page.getByLabel('Запрос').fill('нет-такого');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.getByRole('cell', { name: '@maria' })).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Воронка' }).getByText('Фильтр применён')).toBeVisible();
  await expect(page.getByText('Лидов пока нет')).toBeHidden();
  await expect(page.getByText('По запросу пусто')).toBeVisible();
  await page.getByLabel('Запрос').fill('');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.getByRole('cell', { name: '@maria' })).toBeVisible();
  await expect(page.getByText('По запросу пусто')).toBeHidden();
});

test('F02-E2 emoji and accent search', async ({ page }) => {
  await addLead(page, 'Кафе Émile 😀', 'emile');
  await page.getByLabel('Запрос').fill('Émile');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.getByRole('table').getByLabel('Имя')).toHaveValue('Кафе Émile 😀');
  await page.getByLabel('Запрос').fill('😀');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.getByRole('table').getByLabel('Имя')).toHaveValue('Кафе Émile 😀');
});

test('F02-E3 very long query does not 500 and shows no row', async ({ page }) => {
  await addLead(page, 'Мария', 'maria');
  await page.getByLabel('Запрос').fill('я'.repeat(400));
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.getByRole('cell', { name: '@maria' })).toHaveCount(0);
});

test('F02-N1 Telegram search without an account shows an error', async ({ page }) => {
  await page.getByRole('button', { name: 'В Telegram' }).click();
  await page.getByLabel('Запрос').fill('иван');
  await page.getByRole('button', { name: 'Найти' }).click();
  const inbox = page.getByRole('region', { name: 'Входящие' });
  await expect(inbox.getByRole('alert')).toHaveText('Нет подключённого аккаунта');
});

test('F02-E4 empty query returns every lead', async ({ page }) => {
  await addLead(page, 'Иван Петров', 'ivan');
  await addLead(page, 'Мария', 'maria');
  await page.getByLabel('Запрос').fill('иван');
  await page.getByRole('button', { name: 'Найти' }).click();
  await page.getByLabel('Запрос').fill('');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.getByRole('cell', { name: '@ivan' })).toBeVisible();
  await expect(page.getByRole('cell', { name: '@maria' })).toBeVisible();
});

test('F02-H4 cached dialogs open without another Telegram search', async ({ page }) => {
  await connect(page);
  await page.getByRole('button', { name: 'В Telegram' }).click();
  await page.getByLabel('Запрос').fill('@ivan');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.getByRole('button', { name: /Иван Петров/ })).toBeVisible();
  let dialogHits = 0;
  page.on('request', (req) => {
    if (req.method() === 'GET' && /\/dialogs(\?|$)/.test(req.url())) dialogHits += 1;
  });
  await page.reload();
  await expect(page.getByRole('button', { name: /Иван Петров/ })).toBeVisible();
  expect(dialogHits).toBe(0);
});

test('F02-E6 send shows the server error and no bubble', async ({ page }) => {
  await connect(page);
  await page.getByRole('button', { name: 'В Telegram' }).click();
  await page.getByLabel('Запрос').fill('@olga');
  await page.getByRole('button', { name: 'Найти' }).click();
  await page.getByRole('button', { name: /Ольга/ }).click();
  await page.route('**/api/dialogs/*/messages', (route) => route.fulfill({
    status: 400,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'Аккаунт не подключён' }),
  }));
  const thread = page.getByRole('region', { name: 'Переписка' });
  await thread.getByLabel('Ответ').fill('не уйдёт');
  await thread.getByRole('button', { name: 'Отправить' }).click();
  await expect(thread.getByRole('alert')).toHaveText('Аккаунт не подключён');
  await expect(thread.getByRole('list')).toBeEmpty();
  await expect(thread.getByText('не уйдёт')).toHaveCount(0);
});

test('F02-H5 personal history sits beside the card', async ({ page }) => {
  await connect(page);
  await page.getByRole('button', { name: 'В Telegram' }).click();
  await page.getByLabel('Запрос').fill('@petr');
  await page.getByRole('button', { name: 'Найти' }).click();
  await page.getByRole('button', { name: /Пётр/ }).click();
  const thread = page.getByRole('region', { name: 'Переписка' });
  const card = page.getByRole('region', { name: 'Рядом с чатом' });
  await expect(thread.getByText('был в телеграме')).toBeVisible();
  await expect(card.getByText('К этому чату лид ещё не заведён')).toBeVisible();
  await expect(thread.getByText('Сообщений пока нет')).toBeHidden();
  await thread.getByLabel('Ответ').fill('ответ петру');
  await thread.getByRole('button', { name: 'Отправить' }).click();
  await expect(thread.getByText('был в телеграме')).toBeVisible();
  await expect(thread.getByText('ответ петру')).toBeVisible();
  await expect(thread.getByRole('listitem')).toHaveCount(2);
  await expect(card.getByRole('button', { name: 'В лиды' })).toBeVisible();
});

test('F02-H3 Telegram name search finds the accented dialog', async ({ page }) => {
  await connect(page);
  await page.getByRole('button', { name: 'В Telegram' }).click();
  await page.getByLabel('Запрос').fill('Émile');
  await page.getByRole('button', { name: 'Найти' }).click();
  await expect(page.getByRole('button', { name: /Émile/ })).toBeVisible();
  await page.getByRole('button', { name: 'Непрочитанные' }).click();
  await expect(page.getByText('В ответе поиска нет этого признака, поэтому список может опустеть.')).toBeVisible();
});
