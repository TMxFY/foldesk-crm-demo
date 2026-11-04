import { defineConfig } from '@playwright/test';

const port = process.env.E2E_PORT || '18765';
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: '.',
  testMatch: /.*\.spec\.ts/,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  reporter: [['list']],
  use: {
    baseURL,
    viewport: { width: 1440, height: 900 },
    trace: 'retain-on-failure',
  },
  webServer: {
    command: '../.venv/bin/python serve.py',
    cwd: '.',
    url: `${baseURL}/api/accounts`,
    reuseExistingServer: false,
    timeout: 30_000,
    env: { ...process.env, PORT: port },
  },
});
