import { expect, type Page, type TestInfo } from '@playwright/test';

const problems: string[] = [];

export function watch(page: Page) {
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    // Chromium logs expected 4xx (wrong code, empty lead) as a console error.
    if (/Failed to load resource: the server responded with a status of 4\d\d/.test(text)) return;
    problems.push(`console error: ${text}`);
  });
  page.on('pageerror', (err) => {
    problems.push(`pageerror: ${err.message}`);
  });
  page.on('response', (res) => {
    if (res.status() >= 500) problems.push(`${res.status()} on ${res.url()}`);
  });
}

export function resetProblems() {
  problems.length = 0;
}

export function assertQuiet() {
  expect(problems, problems.join('\n')).toEqual([]);
}

export async function reset(page: Page) {
  const res = await page.request.post('/api/test/reset');
  expect(res.ok(), await res.text()).toBeTruthy();
}

export async function scan(page: Page, screen: string) {
  let AxeBuilder: typeof import('@axe-core/playwright').default;
  try {
    AxeBuilder = (await import('@axe-core/playwright')).default;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (message.includes('Cannot find module') || message.includes('Cannot find package')) {
      return 'axe package is not installed';
    }
    throw err;
  }
  const result = await new AxeBuilder({ page }).analyze();
  const lines = result.violations.map((item) => {
    const targets = item.nodes.map((node) => node.target.join(' ')).join(' | ');
    return `${item.impact} ${item.id}: ${item.help} :: ${targets}`;
  });
  expect(lines, `${screen}\n${lines.join('\n')}`).toEqual([]);
  return 'axe ok';
}

export function noteAxe(info: TestInfo, status: string) {
  info.annotations.push({ type: 'axe', description: status });
}

export async function connect(page: Page, phone = '+79990000000') {
  const accounts = page.getByRole('region', { name: 'Аккаунт' });
  await accounts.getByLabel('Телефон').fill(phone);
  await accounts.getByRole('button', { name: 'Получить код' }).click();
  await accounts.getByLabel('Код').fill('11111');
  await accounts.getByRole('button', { name: 'Подтвердить код' }).click();
  if (phone.endsWith('0002')) {
    await expect(accounts.getByLabel('Пароль 2FA')).toBeVisible();
    return;
  }
  await expect(accounts.getByText('на связи')).toBeVisible();
  await expect(accounts.getByText('Оператор')).toBeVisible();
}

export async function addLead(page: Page, name: string, username = '') {
  const leads = page.getByRole('region', { name: 'Лиды' });
  await leads.getByLabel('Имя').first().fill(name);
  if (username) await leads.getByLabel('Ник').fill(username);
  await leads.getByRole('button', { name: 'Завести карточку' }).click();
  await expect(page.getByRole('table').getByLabel('Имя').first()).toHaveValue(name);
}

export function column(page: Page, title: string) {
  return page
    .getByRole('heading', { level: 3, name: title, exact: true })
    .locator('xpath=ancestor::section[1]');
}
