// @ts-check
const { defineConfig, devices } = require('@playwright/test');

/**
 * DC Tech Events Browser Test Configuration
 * 
 * RUNNING TESTS:
 * 
 * 1. Unauthenticated tests only:
 *    npx playwright test tests/browser.spec.js
 * 
 * 2. Set up authentication first:
 *    TEST_USER_EMAIL="user@example.com" \
 *    TEST_USER_PASSWORD="userpass" \
 *    TEST_ADMIN_EMAIL="admin@example.com" \
 *    TEST_ADMIN_PASSWORD="adminpass" \
 *    npx playwright test --project=setup
 * 
 * 3. Run authenticated tests:
 *    npx playwright test tests/authenticated.spec.js
 *    npx playwright test tests/admin.spec.js
 * 
 * 4. Run all tests:
 *    npx playwright test
 * 
 * @see https://playwright.dev/docs/test-configuration
 */
module.exports = defineConfig({
    testDir: './tests',

    /* Run tests in files in parallel */
    fullyParallel: true,

    /* Fail the build on CI if you accidentally left test.only in the source code. */
    forbidOnly: !!process.env.CI,

    /* Retry on CI only */
    retries: process.env.CI ? 2 : 0,

    /* Opt out of parallel tests on CI. */
    workers: process.env.CI ? 1 : undefined,

    /* Reporter to use. See https://playwright.dev/docs/test-reporters */
    reporter: [
        ['html'],
        ['list'],
    ],

    /* Shared settings for all the projects below. See https://playwright.dev/docs/api/class-testoptions. */
    use: {
        /* Base URL to use in actions like `await page.goto('/')`. */
        baseURL: process.env.TEST_URL || 'https://next.dctech.events',

        /* Collect trace when retrying the failed test. See https://playwright.dev/docs/trace-viewer */
        trace: 'on-first-retry',

        /* Take screenshot on failure */
        screenshot: 'only-on-failure',
    },

    /* Configure projects for major browsers */
    projects: [
        // Setup project for authentication
        {
            name: 'setup',
            testMatch: /global-setup\.js/,
            teardown: 'cleanup',
        },
        {
            name: 'cleanup',
            testMatch: /global-teardown\.js/,
        },

        // Unauthenticated tests (public pages)
        {
            name: 'chromium',
            use: { ...devices['Desktop Chrome'] },
            testMatch: 'browser.spec.js',
        },
        {
            name: 'firefox',
            use: { ...devices['Desktop Firefox'] },
            testMatch: 'browser.spec.js',
        },
        {
            name: 'webkit',
            use: { ...devices['Desktop Safari'] },
            testMatch: 'browser.spec.js',
        },

        // Authenticated user tests
        {
            name: 'authenticated',
            use: {
                ...devices['Desktop Chrome'],
                storageState: 'tests/auth/user.json',
            },
            testMatch: 'authenticated.spec.js',
            dependencies: ['setup'],
        },

        // Admin user tests
        {
            name: 'admin',
            use: {
                ...devices['Desktop Chrome'],
                storageState: 'tests/auth/admin.json',
            },
            testMatch: 'admin.spec.js',
            dependencies: ['setup'],
        },

        // Mobile tests
        {
            name: 'Mobile Chrome',
            use: { ...devices['Pixel 5'] },
            testMatch: 'browser.spec.js',
        },
        {
            name: 'Mobile Safari',
            use: { ...devices['iPhone 12'] },
            testMatch: 'browser.spec.js',
        },
    ],

    /* Folder for test artifacts such as screenshots, videos, traces, etc. */
    outputDir: 'test-results/',

    /* Maximum time one test can run for. */
    timeout: 30000,
});
