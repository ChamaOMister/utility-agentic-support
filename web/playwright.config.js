import { defineConfig, devices } from '@playwright/test';
export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  timeout: 30000,
  expect: {timeout: 10000},
  reporter: 'list',
  use: {baseURL: 'http://localhost:5173', trace: 'retain-on-failure'},
  projects: [{name: 'chromium', use: {...devices['Desktop Chrome']}}],
});
