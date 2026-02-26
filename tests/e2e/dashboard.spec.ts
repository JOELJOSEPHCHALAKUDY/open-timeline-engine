import { expect, test } from '@playwright/test';

const dashboardUrl = process.env.TCE_DASHBOARD_URL ?? 'http://localhost:8080/dashboard';

test.describe('dashboard control plane', () => {
  test('loads overview and primary navigation', async ({ page }) => {
    await page.goto(dashboardUrl);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
    await expect(page.getByRole('link', { name: /Timeline/i })).toBeVisible();
    await expect(page.getByRole('link', { name: /Takeover/i })).toBeVisible();
    await expect(page.getByRole('link', { name: /Graph/i })).toBeVisible();
    await expect(page.getByRole('link', { name: /Patterns/i })).toBeVisible();
    await expect(page.getByRole('link', { name: /Settings/i })).toBeVisible();
  });

  test('can navigate to takeover and patterns pages', async ({ page }) => {
    await page.goto(dashboardUrl);
    await page.getByRole('link', { name: /Takeover/i }).click();
    await expect(page.getByRole('heading', { name: 'Takeover' })).toBeVisible();

    await page.getByRole('link', { name: /Patterns/i }).click();
    await expect(page.getByRole('heading', { name: 'Pattern Review' })).toBeVisible();
  });
});
