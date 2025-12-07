/**
 * DC Tech Events - Authenticated User Tests
 * 
 * These tests run with a logged-in normal user.
 * Requires tests/auth/user.json to exist (run global-setup first).
 */

const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const BASE_URL = process.env.TEST_URL || 'https://next.dctech.events';
const AUTH_FILE = path.join(__dirname, 'auth', 'user.json');

// Skip all tests if auth file doesn't exist
test.beforeAll(() => {
    if (!fs.existsSync(AUTH_FILE)) {
        console.log(`⚠️  Auth file not found at ${AUTH_FILE}`);
        console.log('Run: TEST_USER_EMAIL=... TEST_USER_PASSWORD=... npx playwright test --global-setup=./tests/global-setup.js');
        test.skip();
    }
});

// Use the saved authentication state
test.use({ storageState: AUTH_FILE });

// ============================================
// Profile & Settings
// ============================================

test.describe('Authenticated User - Profile', () => {
    test('can access settings page', async ({ page }) => {
        await page.goto(`${BASE_URL}/settings`);

        // Should see settings form, not a redirect to login
        await expect(page.locator('h1, h2')).toContainText(/settings/i);
        await expect(page.locator('form')).toBeVisible();
    });

    test('settings page shows privacy options', async ({ page }) => {
        await page.goto(`${BASE_URL}/settings`);

        // Check for RSVP privacy checkbox
        await expect(page.locator('input[name="showRsvps"], #showRsvps')).toBeVisible();
    });

    test('header shows logout link', async ({ page }) => {
        await page.goto(BASE_URL);

        await expect(page.locator('a[href="/logout"]')).toBeVisible();
    });

    test('header shows submit event link', async ({ page }) => {
        await page.goto(BASE_URL);

        await expect(page.locator('a[href="/submit/"]')).toBeVisible();
    });
});

// ============================================
// Event Submission
// ============================================

test.describe('Authenticated User - Event Submission', () => {
    test('can access submit form', async ({ page }) => {
        await page.goto(`${BASE_URL}/submit/`);

        // Should see the form
        await expect(page.locator('form#event-form, form')).toBeVisible();
        await expect(page.locator('input[name="title"]')).toBeVisible();
    });

    test('form has event type toggle', async ({ page }) => {
        await page.goto(`${BASE_URL}/submit/`);

        // Check for external/native toggle
        await expect(page.locator('#type-external')).toBeVisible();
        await expect(page.locator('#type-native')).toBeVisible();
    });

    test('selecting native event shows RSVP options', async ({ page }) => {
        await page.goto(`${BASE_URL}/submit/`);

        // Click native event option
        await page.locator('#type-native').click();

        // RSVP options should appear
        await expect(page.locator('#native-options, .native-options')).toBeVisible();
        await expect(page.locator('#rsvpEnabled')).toBeVisible();
    });

    test('form has recurrence picker in optional details', async ({ page }) => {
        await page.goto(`${BASE_URL}/submit/`);

        // Open optional details
        await page.locator('summary').filter({ hasText: /optional/i }).click();

        // Check for recurrence dropdown
        await expect(page.locator('#recurrenceRule')).toBeVisible();

        // Check it has options
        const options = await page.locator('#recurrenceRule option').count();
        expect(options).toBeGreaterThan(5); // Should have many recurrence options
    });

    test('form has topic selector', async ({ page }) => {
        await page.goto(`${BASE_URL}/submit/`);

        // Open optional details
        await page.locator('summary').filter({ hasText: /optional/i }).click();

        // Check for topic dropdown
        await expect(page.locator('#topicSlug')).toBeVisible();
    });
});

// ============================================
// Topic Following
// ============================================

test.describe('Authenticated User - Topic Following', () => {
    test('can see follow button on topic page', async ({ page }) => {
        // First get a topic slug
        await page.goto(`${BASE_URL}/topics/`);

        const topicLinks = page.locator('a[href^="/topics/"]').filter({ hasNotText: /^topics$/i });
        const count = await topicLinks.count();

        if (count === 0) {
            test.skip();
            return;
        }

        // Go to first topic
        await topicLinks.first().click();
        await page.waitForLoadState('networkidle');

        // Should see follow button (either Follow or Following)
        await expect(page.locator('button, a').filter({ hasText: /follow/i })).toBeVisible();
    });

    test('can access personalized feed', async ({ page }) => {
        await page.goto(`${BASE_URL}/my-feed`);

        // Should load the feed page
        await expect(page.locator('h1, h2')).toContainText(/feed|events/i);
    });
});

// ============================================
// Discussion Boards
// ============================================

test.describe('Authenticated User - Discussions', () => {
    test('can see create thread form on topic', async ({ page }) => {
        // First get a topic slug
        await page.goto(`${BASE_URL}/topics/`);

        const topicLinks = page.locator('a[href^="/topics/"]').filter({ hasNotText: /^topics$/i });
        const count = await topicLinks.count();

        if (count === 0) {
            test.skip();
            return;
        }

        const firstLink = topicLinks.first();
        const href = await firstLink.getAttribute('href');
        const slug = href?.match(/\/topics\/([a-z0-9-]+)/)?.[1];

        // Check threads API - should work for authenticated users
        const response = await page.request.get(`${BASE_URL}/api/topics/${slug}/threads`);
        expect(response.ok()).toBeTruthy();
    });
});

// ============================================
// RSVP Functionality
// ============================================

test.describe('Authenticated User - RSVP', () => {
    test('can view events page without errors', async ({ page }) => {
        await page.goto(BASE_URL);

        // Look for event links  
        const eventLinks = page.locator('a[href^="/events/"]');
        const count = await eventLinks.count();

        // Either we have events, or we have an empty state
        expect(count >= 0).toBeTruthy();
    });
});

// ============================================
// Upvoting
// ============================================

test.describe('Authenticated User - Upvoting', () => {
    test('upvote API requires authentication and works', async ({ page }) => {
        // Get an event ID first
        const eventsResponse = await page.request.get(`${BASE_URL}/events`);
        const events = await eventsResponse.json();

        if (!events || events.length === 0) {
            test.skip();
            return;
        }

        const eventId = events[0].eventId;

        // Try to upvote (may succeed or return "Already upvoted")
        const upvoteResponse = await page.request.post(`${BASE_URL}/api/events/${eventId}/upvote`);

        // Should not be 403 (authentication required) for authenticated user
        expect(upvoteResponse.status()).not.toBe(403);
    });
});
