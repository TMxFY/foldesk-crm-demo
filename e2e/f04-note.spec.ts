import { expect, test } from '@playwright/test';
import { addLead, assertQuiet, reset, resetProblems, watch } from './helpers';

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

test('F04-H1 note shows on the timeline and after reload', async ({ page }) => {
  const card = page.getByRole('region', { name: 'Карточка' });
  await card.getByRole('button', { name: 'Заметка' }).click();
  const dialog = page.getByRole('dialog', { name: 'Заметка' });
  await expect(dialog.getByLabel('Текст')).toBeFocused();
  await dialog.getByLabel('Текст').fill('счёт выставлен');
  await dialog.getByRole('button', { name: 'Записать' }).click();
  await expect(dialog).toBeHidden();
  await expect(card.getByText('счёт выставлен')).toBeVisible();
  await page.reload();
  await page.getByRole('cell', { name: '@ivan_p' }).click();
  await expect(card.getByText('счёт выставлен')).toBeVisible();
});

test('F04-E1 empty note stays open and does not save', async ({ page }) => {
  const card = page.getByRole('region', { name: 'Карточка' });
  await card.getByRole('button', { name: 'Заметка' }).click();
  const dialog = page.getByRole('dialog', { name: 'Заметка' });
  await dialog.getByRole('button', { name: 'Записать' }).click();
  await expect(dialog.getByText('Напишите текст')).toBeVisible();
  await expect(dialog).toBeVisible();
  const rows = await (await page.request.get('/api/leads')).json();
  expect(rows[0].note).toBeNull();
  const cardBody = await (await page.request.get(`/api/leads/${rows[0].id}/card`)).json();
  expect(cardBody.notes).toEqual([]);
});

test('F04-E2 emoji note and escape closes without saving', async ({ page }) => {
  const card = page.getByRole('region', { name: 'Карточка' });
  await card.getByRole('button', { name: 'Заметка' }).click();
  const dialog = page.getByRole('dialog', { name: 'Заметка' });
  await dialog.getByLabel('Текст').fill('повтор 😀');
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  let rows = await (await page.request.get('/api/leads')).json();
  expect(rows[0].note).toBeNull();

  await card.getByRole('button', { name: 'Заметка' }).click();
  await dialog.getByLabel('Текст').fill('повтор 😀');
  await dialog.getByRole('button', { name: 'Записать' }).click();
  await expect(card.getByText('повтор 😀')).toBeVisible();
  rows = await (await page.request.get('/api/leads')).json();
  expect(rows[0].note).toBeNull();
  const cardBody = await (await page.request.get(`/api/leads/${rows[0].id}/card`)).json();
  expect(cardBody.notes.map((note: { body: string }) => note.body)).toEqual(['повтор 😀']);
});
