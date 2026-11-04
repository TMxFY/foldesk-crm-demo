import { expect, test } from '@playwright/test';
import { assertQuiet, connect, noteAxe, reset, resetProblems, scan, watch } from './helpers';

test.beforeEach(async ({ page }) => {
  resetProblems();
  watch(page);
  await reset(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Кабинет' })).toBeVisible();
});

test.afterEach(() => {
  assertQuiet();
});

test('F01-H1 connect shows the account as online', async ({ page }, info) => {
  const accounts = page.getByRole('region', { name: 'Аккаунт' });
  await expect(accounts.getByText('Аккаунт ещё не подключён')).toBeVisible();
  noteAxe(info, await scan(page, 'S03 empty'));
  await connect(page);
  await expect(accounts.getByText('на связи')).toBeVisible();
  await expect(accounts.getByText('активен')).toBeVisible();
});

test('F01-H3 reconnect shows health and does not pretend a dead session is fine', async ({ page }) => {
  await connect(page);
  const accounts = page.getByRole('region', { name: 'Аккаунт' });
  await expect(accounts.getByText('активен')).toBeVisible();
  const pending = page.waitForResponse(
    (res) => res.url().includes('/reconnect') && res.request().method() === 'POST',
  );
  await accounts.getByRole('button', { name: 'Переподключить' }).click();
  const response = await pending;
  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(body.health).toBe('active');
  await expect(accounts.getByText('активен')).toBeVisible();
  await expect(accounts.getByText('на связи')).toBeVisible();
  await expect(accounts.getByRole('alert')).toHaveCount(0);
});

test('F01-H4 reconnect shows unauthorized and does not keep the active badge', async ({ page }) => {
  await connect(page);
  const accounts = page.getByRole('region', { name: 'Аккаунт' });
  const primed = await page.request.post('/api/test/session-health', { data: { health: 'unauthorized' } });
  expect(primed.ok()).toBeTruthy();
  await accounts.getByRole('button', { name: 'Переподключить' }).click();
  await expect(accounts.getByText('не авторизован')).toBeVisible();
  await expect(accounts.getByRole('alert')).toHaveText('Сессия недействительна');
  await expect(accounts.getByText('активен')).toHaveCount(0);
  await expect(accounts.getByText('на связи')).toBeVisible();
});

test('F01-N1 wrong code stays on pending and does not say connected', async ({ page }) => {
  const accounts = page.getByRole('region', { name: 'Аккаунт' });
  await accounts.getByLabel('Телефон').fill('+79990000000');
  await accounts.getByRole('button', { name: 'Получить код' }).click();
  await accounts.getByLabel('Код').fill('00000');
  await accounts.getByRole('button', { name: 'Подтвердить код' }).click();
  await expect(accounts.getByRole('alert')).toHaveText('Неверный код');
  await expect(accounts.getByText('ждём код')).toBeVisible();
  await expect(accounts.getByText('на связи')).toHaveCount(0);
});

test('F01-N2 wrong 2FA password then the right one', async ({ page }) => {
  const accounts = page.getByRole('region', { name: 'Аккаунт' });
  await connect(page, '+79990000002');
  await accounts.getByLabel('Пароль 2FA').fill('nope');
  await accounts.getByRole('button', { name: 'Подтвердить пароль' }).click();
  await expect(accounts.getByRole('alert')).toHaveText('Неверный пароль');
  await expect(accounts.getByText('ждём пароль')).toBeVisible();
  await accounts.getByLabel('Пароль 2FA').fill('secret');
  await accounts.getByRole('button', { name: 'Подтвердить пароль' }).click();
  await expect(accounts.getByText('на связи')).toBeVisible();
  await expect(accounts.getByText('Оператор')).toBeVisible();
});

test('F01-E1 empty phone does not create an account', async ({ page }) => {
  const accounts = page.getByRole('region', { name: 'Аккаунт' });
  await accounts.getByLabel('Телефон').fill('');
  await accounts.getByRole('button', { name: 'Получить код' }).click();
  const missing = await accounts.getByLabel('Телефон').evaluate((el: HTMLInputElement) => el.validity.valueMissing);
  expect(missing).toBe(true);
  expect(await (await page.request.get('/api/accounts')).json()).toEqual([]);
});

test('F01-E2 long and emoji codes are rejected without a 500', async ({ page }) => {
  const accounts = page.getByRole('region', { name: 'Аккаунт' });
  await accounts.getByLabel('Телефон').fill('+79990000000');
  await accounts.getByRole('button', { name: 'Получить код' }).click();
  await accounts.getByLabel('Код').fill('1'.repeat(80));
  await accounts.getByRole('button', { name: 'Подтвердить код' }).click();
  await expect(accounts.getByRole('alert')).toHaveText('Неверный код');
  await accounts.getByLabel('Код').fill('код😀');
  await accounts.getByRole('button', { name: 'Подтвердить код' }).click();
  await expect(accounts.getByRole('alert')).toHaveText('Неверный код');
  await expect(accounts.getByText('ждём код')).toBeVisible();
});

test('F01-E4 double click on get-code creates one account', async ({ page }) => {
  const accounts = page.getByRole('region', { name: 'Аккаунт' });
  await accounts.getByLabel('Телефон').fill('+79990001122');
  await accounts.getByRole('button', { name: 'Получить код' }).dblclick();
  await expect(accounts.getByLabel('Код')).toBeVisible();
  const rows = await (await page.request.get('/api/accounts')).json();
  expect(rows).toHaveLength(1);
  expect(rows[0].status).toBe('pending_code');
});

test('F01-E6 refresh while the code is pending keeps the step', async ({ page }) => {
  const accounts = page.getByRole('region', { name: 'Аккаунт' });
  await accounts.getByLabel('Телефон').fill('+79990001122');
  await accounts.getByRole('button', { name: 'Получить код' }).click();
  await expect(accounts.getByLabel('Код')).toBeVisible();
  await page.reload();
  await expect(accounts.getByText('ждём код')).toBeVisible();
  await expect(accounts.getByLabel('Код')).toBeVisible();
  await accounts.getByLabel('Код').fill('11111');
  await accounts.getByRole('button', { name: 'Подтвердить код' }).click();
  await expect(accounts.getByText('на связи')).toBeVisible();
});

test('F01-E7 slow code request still ends on the code field', async ({ page }) => {
  await page.route('**/api/accounts/start', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 400));
    await route.continue();
  });
  const accounts = page.getByRole('region', { name: 'Аккаунт' });
  const button = accounts.getByRole('button', { name: 'Получить код' });
  await accounts.getByLabel('Телефон').fill('+79990001122');
  const pending = button.click();
  await expect(button).toBeDisabled();
  await pending;
  await expect(accounts.getByLabel('Код')).toBeVisible();
});

test('F01-E12 mobile width still shows connect and digest', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await expect(page.getByRole('heading', { name: 'Аккаунт' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Входящие' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Напоминания' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Аккаунт' }).getByLabel('Телефон')).toBeVisible();
});

test('F01-E13 keyboard submits the phone form', async ({ page }) => {
  const phone = page.getByRole('region', { name: 'Аккаунт' }).getByLabel('Телефон');
  await phone.focus();
  await page.keyboard.type('+79990001100');
  await page.keyboard.press('Enter');
  await expect(page.getByLabel('Код')).toBeVisible();
});

test('F01-E5 back from a section does not drop the account', async ({ page }) => {
  await connect(page);
  await page.getByRole('link', { name: 'Напоминания' }).click();
  await expect(page).toHaveURL(/#digest/);
  await page.goBack();
  await expect(page.getByRole('region', { name: 'Аккаунт' }).getByText('на связи')).toBeVisible();
});

test('F01-N3 missing account returns 404 and not a success', async ({ page }) => {
  const res = await page.request.post('/api/accounts/999999/code', { data: { code: '11111' } });
  expect(res.status()).toBe(404);
  await expect(page.getByText('на связи')).toHaveCount(0);
});

test('F01-E3 second tab sees the same connected account', async ({ page }) => {
  await connect(page);
  const other = await page.context().newPage();
  watch(other);
  await other.goto('/');
  await expect(other.getByRole('region', { name: 'Аккаунт' }).getByText('на связи')).toBeVisible();
  await other.close();
});
