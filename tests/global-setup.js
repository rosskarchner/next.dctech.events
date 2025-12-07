/**
 * Playwright Global Setup for Authenticated Tests
 * 
 * This script runs before tests to create authentication states for:
 * - Normal user: tests/auth/user.json
 * - Admin user: tests/auth/admin.json
 * 
 * Set these environment variables before running:
 * - TEST_USER_EMAIL: Normal user's email
 * - TEST_USER_PASSWORD: Normal user's password
 * - TEST_ADMIN_EMAIL: Admin user's email  
 * - TEST_ADMIN_PASSWORD: Admin user's password
 * 
 * Run with: npx playwright test --global-setup=./tests/global-setup.js
 */

const { chromium } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const BASE_URL = process.env.TEST_URL || 'https://next.dctech.events';

async function globalSetup() {
    // Ensure auth directory exists
    const authDir = path.join(__dirname, 'auth');
    if (!fs.existsSync(authDir)) {
        fs.mkdirSync(authDir, { recursive: true });
    }

    const browser = await chromium.launch({ headless: false }); // Set to true for CI

    // ============================================
    // Authenticate Normal User
    // ============================================
    if (process.env.TEST_USER_EMAIL && process.env.TEST_USER_PASSWORD) {
        console.log('Authenticating normal user...');
        const userContext = await browser.newContext();
        const userPage = await userContext.newPage();

        try {
            // Navigate to login
            await userPage.goto(`${BASE_URL}/login`);

            // Wait for Cognito login page
            await userPage.waitForURL(/cognito|amazoncognito/, { timeout: 10000 });

            // Fill Cognito login form
            await userPage.fill('input[name="username"], input[type="email"]', process.env.TEST_USER_EMAIL);
            await userPage.fill('input[name="password"], input[type="password"]', process.env.TEST_USER_PASSWORD);
            await userPage.click('button[type="submit"], input[type="submit"]');

            // Wait for redirect back to app
            await userPage.waitForURL(url => url.hostname === 'next.dctech.events' || url.hostname === 'localhost', { timeout: 30000 });

            // Save storage state
            await userContext.storageState({ path: path.join(authDir, 'user.json') });
            console.log('✅ Normal user authentication saved to tests/auth/user.json');
        } catch (error) {
            console.error('❌ Failed to authenticate normal user:', error.message);
        }

        await userContext.close();
    } else {
        console.log('⚠️  Skipping normal user auth (TEST_USER_EMAIL/TEST_USER_PASSWORD not set)');
    }

    // ============================================
    // Authenticate Admin User
    // ============================================
    if (process.env.TEST_ADMIN_EMAIL && process.env.TEST_ADMIN_PASSWORD) {
        console.log('Authenticating admin user...');
        const adminContext = await browser.newContext();
        const adminPage = await adminContext.newPage();

        try {
            // Navigate to login
            await adminPage.goto(`${BASE_URL}/login`);

            // Wait for Cognito login page
            await adminPage.waitForURL(/cognito|amazoncognito/, { timeout: 10000 });

            // Fill Cognito login form
            await adminPage.fill('input[name="username"], input[type="email"]', process.env.TEST_ADMIN_EMAIL);
            await adminPage.fill('input[name="password"], input[type="password"]', process.env.TEST_ADMIN_PASSWORD);
            await adminPage.click('button[type="submit"], input[type="submit"]');

            // Wait for redirect back to app
            await adminPage.waitForURL(url => url.hostname === 'next.dctech.events' || url.hostname === 'localhost', { timeout: 30000 });

            // Save storage state
            await adminContext.storageState({ path: path.join(authDir, 'admin.json') });
            console.log('✅ Admin user authentication saved to tests/auth/admin.json');
        } catch (error) {
            console.error('❌ Failed to authenticate admin user:', error.message);
        }

        await adminContext.close();
    } else {
        console.log('⚠️  Skipping admin user auth (TEST_ADMIN_EMAIL/TEST_ADMIN_PASSWORD not set)');
    }

    await browser.close();
}

module.exports = globalSetup;
