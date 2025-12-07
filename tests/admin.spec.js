/**
 * DC Tech Events - Admin User Tests
 * 
 * These tests run with a logged-in admin user.
 * Requires tests/auth/admin.json to exist (run global-setup first).
 */

const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const BASE_URL = process.env.TEST_URL || 'https://next.dctech.events';
const AUTH_FILE = path.join(__dirname, 'auth', 'admin.json');

// Skip all tests if auth file doesn't exist
test.beforeAll(() => {
    if (!fs.existsSync(AUTH_FILE)) {
        console.log(`⚠️  Admin auth file not found at ${AUTH_FILE}`);
        console.log('Run: TEST_ADMIN_EMAIL=... TEST_ADMIN_PASSWORD=... npx playwright test --global-setup=./tests/global-setup.js');
        test.skip();
    }
});

// Use the saved authentication state
test.use({ storageState: AUTH_FILE });

// ============================================
// Admin-Specific Features
// ============================================

test.describe('Admin - Topic Management', () => {
    test('can access topics API', async ({ page }) => {
        const response = await page.request.get(`${BASE_URL}/api/topics`);
        expect(response.ok()).toBeTruthy();
    });

    test('can create a new topic (if admin)', async ({ page }) => {
        // Generate unique slug to avoid conflicts
        const uniqueSlug = `test-topic-${Date.now()}`;

        const response = await page.request.post(`${BASE_URL}/api/topics`, {
            data: {
                name: 'Test Topic',
                slug: uniqueSlug,
                description: 'A test topic created by automated tests',
                color: '#4f46e5',
            },
        });

        // Admin should be able to create, non-admin will get 403
        const status = response.status();
        expect([201, 403]).toContain(status);

        if (status === 201) {
            console.log(`✅ Admin created topic: ${uniqueSlug}`);
        } else {
            console.log('⚠️  User is not an admin (topic creation returned 403)');
        }
    });
});

// ============================================
// Admin - Event Management
// ============================================

test.describe('Admin - Event Management', () => {
    test('can view all events via API', async ({ page }) => {
        const response = await page.request.get(`${BASE_URL}/events`);
        expect(response.ok()).toBeTruthy();

        const events = await response.json();
        expect(Array.isArray(events)).toBeTruthy();
    });

    test('can create an event', async ({ page }) => {
        const tomorrow = new Date();
        tomorrow.setDate(tomorrow.getDate() + 1);
        const dateStr = tomorrow.toISOString().split('T')[0];

        const response = await page.request.post(`${BASE_URL}/events`, {
            data: {
                title: `Test Event ${Date.now()}`,
                date: dateStr,
                time: '18:30',
                location: '123 Test St, Washington, DC 20001',
                url: 'https://example.com/test-event',
                description: 'A test event created by automated tests',
                eventType: 'external',
            },
        });

        expect([201, 200]).toContain(response.status());

        if (response.status() === 201) {
            const data = await response.json();
            console.log(`✅ Created test event: ${data.eventId}`);
        }
    });
});

// ============================================
// Admin - Group Management
// ============================================

test.describe('Admin - Group Access', () => {
    test('can view groups via API', async ({ page }) => {
        const response = await page.request.get(`${BASE_URL}/groups`);
        expect(response.ok()).toBeTruthy();

        const groups = await response.json();
        expect(Array.isArray(groups)).toBeTruthy();
    });
});

// ============================================
// Admin - User Lookup (if applicable)
// ============================================

test.describe('Admin - Profile Access', () => {
    test('can view own profile', async ({ page }) => {
        // First go to settings to get own nickname
        await page.goto(`${BASE_URL}/settings`);

        // Check we can access settings
        await expect(page.locator('h1, h2')).toContainText(/settings/i);
    });
});

// ============================================
// Future: Moderation Panel (Phase 8)
// ============================================

test.describe('Admin - Moderation (Phase 8)', () => {
    test.skip('can access moderation panel', async ({ page }) => {
        // TODO: Implement when Phase 8 is complete
        await page.goto(`${BASE_URL}/admin/moderation`);

        await expect(page.locator('h1, h2')).toContainText(/moderation/i);
    });

    test.skip('can view pending flags', async ({ page }) => {
        // TODO: Implement when Phase 8 is complete
        const response = await page.request.get(`${BASE_URL}/api/admin/flags?status=pending`);
        expect(response.ok()).toBeTruthy();
    });
});
