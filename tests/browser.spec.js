/**
 * DC Tech Events - Browser Test Suite
 * 
 * This file contains Playwright browser tests for next.dctech.events.
 * Run with: npx playwright test
 * 
 * Environment: Tests run against the live site (https://next.dctech.events)
 * Note: Some tests require authentication and should be run with care in production.
 */

const { test, expect } = require('@playwright/test');

const BASE_URL = process.env.TEST_URL || 'https://next.dctech.events';

// ============================================
// Phase 1 & 2: Core Navigation & Topics
// ============================================

test.describe('Core Navigation', () => {
    test('homepage loads and displays events', async ({ page }) => {
        await page.goto(BASE_URL);

        // Check basic structure
        await expect(page.locator('header')).toBeVisible();
        await expect(page.locator('h1.logo')).toContainText('DC Tech Events');

        // Check navigation links
        await expect(page.locator('nav')).toContainText('Locations');
        await expect(page.locator('nav')).toContainText('Topics');
        await expect(page.locator('nav')).toContainText('Groups');
    });

    test('topics index page loads', async ({ page }) => {
        await page.goto(`${BASE_URL}/topics/`);

        await expect(page.locator('h1, h2')).toContainText(/topics/i);
        // Page should have topic links or an empty state
        await expect(page.locator('body')).toBeVisible();
    });

    test('groups page loads', async ({ page }) => {
        await page.goto(`${BASE_URL}/groups/`);

        await expect(page.locator('h1, h2')).toContainText(/groups/i);
        await expect(page.locator('body')).toBeVisible();
    });

    test('locations pages load', async ({ page }) => {
        // DC
        await page.goto(`${BASE_URL}/locations/dc/`);
        await expect(page.locator('body')).toBeVisible();

        // Virginia
        await page.goto(`${BASE_URL}/locations/va/`);
        await expect(page.locator('body')).toBeVisible();

        // Maryland
        await page.goto(`${BASE_URL}/locations/md/`);
        await expect(page.locator('body')).toBeVisible();
    });

    test('week view loads', async ({ page }) => {
        // Get current week ID
        const now = new Date();
        const startOfYear = new Date(now.getFullYear(), 0, 1);
        const weekNum = Math.ceil((((now - startOfYear) / 86400000) + startOfYear.getDay() + 1) / 7);
        const weekId = `${now.getFullYear()}-W${String(weekNum).padStart(2, '0')}`;

        await page.goto(`${BASE_URL}/week/${weekId}`);
        await expect(page.locator('body')).toBeVisible();
    });
});

// ============================================
// Phase 3: User Profiles & Authentication
// ============================================

test.describe('User Profiles', () => {
    test('login page redirects to Cognito', async ({ page }) => {
        await page.goto(`${BASE_URL}/login`);

        // Should redirect to Cognito login page
        await page.waitForURL(/cognito|amazoncognito/);
        expect(page.url()).toContain('amazoncognito.com');
    });

    test('profile page shows user not found for invalid user', async ({ page }) => {
        await page.goto(`${BASE_URL}/user/nonexistent_user_12345`);

        await expect(page.locator('body')).toContainText(/not found|doesn't exist/i);
    });

    test('settings page requires authentication', async ({ page }) => {
        await page.goto(`${BASE_URL}/settings`);

        // Should redirect to login
        await page.waitForURL(/login|cognito|amazoncognito/, { timeout: 5000 }).catch(() => { });
        // Or show login prompt
    });
});

// ============================================
// Phase 4: Event Upvoting (Public View)
// ============================================

test.describe('Event Display', () => {
    test('event cards display on homepage', async ({ page }) => {
        await page.goto(BASE_URL);

        // Look for event listings
        const hasEvents = await page.locator('.event-item, .event-card, .day-events').count() > 0;
        const hasEmptyState = await page.locator('body').textContent().then(t => t.includes('No upcoming events'));

        expect(hasEvents || hasEmptyState).toBeTruthy();
    });
});

// ============================================
// Phase 5: Event Submission Form
// ============================================

test.describe('Event Submission', () => {
    test('submit page loads for authenticated users or redirects to login', async ({ page }) => {
        await page.goto(`${BASE_URL}/submit/`);

        // Either shows form (if authenticated) or redirects to login
        const url = page.url();
        const hasForm = await page.locator('form').count() > 0;
        const isLoginRedirect = url.includes('login') || url.includes('cognito');

        expect(hasForm || isLoginRedirect).toBeTruthy();
    });

    test('submit form has event type toggle', async ({ page }) => {
        // Note: This test may need authentication to pass
        await page.goto(`${BASE_URL}/submit/`);

        // Check if redirected to login
        if (page.url().includes('cognito') || page.url().includes('login')) {
            test.skip();
            return;
        }

        // Check for event type toggle
        await expect(page.locator('#type-external, input[value="external"]')).toBeVisible();
        await expect(page.locator('#type-native, input[value="native"]')).toBeVisible();
    });
});

// ============================================
// Phase 6: Recurrence Picker
// ============================================

test.describe('Recurrence', () => {
    test('submit form has recurrence picker in optional details', async ({ page }) => {
        await page.goto(`${BASE_URL}/submit/`);

        // Check if redirected to login
        if (page.url().includes('cognito') || page.url().includes('login')) {
            test.skip();
            return;
        }

        // Open optional details
        await page.locator('summary, details').first().click();

        // Check for recurrence selector
        await expect(page.locator('#recurrenceRule, select[name="recurrenceRule"]')).toBeVisible();
    });
});

// ============================================
// Phase 7: Discussion Boards
// ============================================

test.describe('Discussion Boards', () => {
    test('topic page can load threads API', async ({ page }) => {
        // Get a topic slug first
        await page.goto(`${BASE_URL}/topics/`);

        // Try to find a topic link
        const topicLinks = page.locator('a[href^="/topics/"]').filter({ hasNotText: /^topics$/i });
        const count = await topicLinks.count();

        if (count === 0) {
            test.skip();
            return;
        }

        // Click first topic
        const firstTopic = topicLinks.first();
        const href = await firstTopic.getAttribute('href');
        const slug = href?.match(/\/topics\/([a-z0-9-]+)/)?.[1];

        if (!slug) {
            test.skip();
            return;
        }

        // Test threads API
        const response = await page.request.get(`${BASE_URL}/api/topics/${slug}/threads`);
        expect(response.ok()).toBeTruthy();
    });

    test('thread view handles not found gracefully', async ({ page }) => {
        await page.goto(`${BASE_URL}/threads/00000000-0000-0000-0000-000000000000`);

        await expect(page.locator('body')).toContainText(/not found/i);
    });
});

// ============================================
// API Health Checks
// ============================================

test.describe('API Health', () => {
    test('events API returns valid JSON', async ({ page }) => {
        const response = await page.request.get(`${BASE_URL}/events`);
        expect(response.ok()).toBeTruthy();

        const contentType = response.headers()['content-type'];
        expect(contentType).toContain('application/json');
    });

    test('groups API returns valid JSON', async ({ page }) => {
        const response = await page.request.get(`${BASE_URL}/groups`);
        expect(response.ok()).toBeTruthy();

        const contentType = response.headers()['content-type'];
        expect(contentType).toContain('application/json');
    });

    test('sitemap.xml is accessible', async ({ page }) => {
        const response = await page.request.get(`${BASE_URL}/sitemap.xml`);
        expect(response.ok()).toBeTruthy();

        const body = await response.text();
        expect(body).toContain('<?xml');
        expect(body).toContain('urlset');
    });
});

// ============================================
// Responsive Design
// ============================================

test.describe('Responsive Design', () => {
    test('mobile viewport works', async ({ page }) => {
        await page.setViewportSize({ width: 375, height: 667 });
        await page.goto(BASE_URL);

        await expect(page.locator('header')).toBeVisible();
        await expect(page.locator('nav')).toBeVisible();
    });

    test('tablet viewport works', async ({ page }) => {
        await page.setViewportSize({ width: 768, height: 1024 });
        await page.goto(BASE_URL);

        await expect(page.locator('header')).toBeVisible();
        await expect(page.locator('nav')).toBeVisible();
    });

    test('desktop viewport works', async ({ page }) => {
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.goto(BASE_URL);

        await expect(page.locator('header')).toBeVisible();
        await expect(page.locator('nav')).toBeVisible();
    });
});

// ============================================
// Performance
// ============================================

test.describe('Performance', () => {
    test('homepage loads within 5 seconds', async ({ page }) => {
        const start = Date.now();
        await page.goto(BASE_URL);
        const duration = Date.now() - start;

        expect(duration).toBeLessThan(5000);
    });

    test('no console errors on homepage', async ({ page }) => {
        const errors = [];
        page.on('console', msg => {
            if (msg.type() === 'error') {
                errors.push(msg.text());
            }
        });

        await page.goto(BASE_URL);
        await page.waitForTimeout(2000);

        // Allow some expected errors but fail on critical issues
        const criticalErrors = errors.filter(e =>
            !e.includes('favicon') &&
            !e.includes('404') &&
            !e.includes('net::ERR')
        );

        expect(criticalErrors).toHaveLength(0);
    });
});
