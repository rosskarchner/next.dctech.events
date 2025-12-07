# Browser Testing Guide

This document provides detailed instructions for running the Playwright browser test suite for [next.dctech.events](https://next.dctech.events).

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Test Structure](#test-structure)
4. [Running Public Tests](#running-public-tests)
5. [Setting Up Authenticated Tests](#setting-up-authenticated-tests)
6. [Running Authenticated Tests](#running-authenticated-tests)
7. [Test Coverage by Phase](#test-coverage-by-phase)
8. [CI/CD Integration](#cicd-integration)
9. [Troubleshooting](#troubleshooting)
10. [Writing New Tests](#writing-new-tests)

---

## Prerequisites

- **Node.js** 18+ installed
- **npm** or **yarn** package manager
- Access to the test target URL (default: `https://next.dctech.events`)
- For authenticated tests: Cognito user accounts (regular user and/or admin)

---

## Installation

### 1. Install Playwright

From the project root directory:

```bash
npm install -D @playwright/test
```

### 2. Install Browsers

Playwright needs to download browser binaries:

```bash
npx playwright install
```

This installs Chromium, Firefox, and WebKit browsers.

### 3. Verify Installation

```bash
npx playwright --version
```

---

## Test Structure

The test suite is organized into three files:

```
tests/
├── browser.spec.js        # Public/unauthenticated tests
├── authenticated.spec.js  # Normal user tests
├── admin.spec.js          # Admin user tests
├── global-setup.js        # Authentication setup script
├── README.md              # Quick reference
└── auth/                  # Saved authentication states (gitignored)
    ├── user.json
    └── admin.json
```

### Test Categories

| File | Authentication Required | Description |
|------|------------------------|-------------|
| `browser.spec.js` | No | Tests public pages, navigation, API health, responsive design |
| `authenticated.spec.js` | Yes (normal user) | Tests settings, event submission, following, upvoting |
| `admin.spec.js` | Yes (admin user) | Tests admin features like topic creation, moderation |

---

## Running Public Tests

Public tests don't require any authentication. They test what anonymous visitors can see and do.

### Run All Public Tests

```bash
npx playwright test tests/browser.spec.js
```

### Run in Specific Browser

```bash
# Chrome only
npx playwright test tests/browser.spec.js --project=chromium

# Firefox only
npx playwright test tests/browser.spec.js --project=firefox

# Safari only
npx playwright test tests/browser.spec.js --project=webkit
```

### Run in Headed Mode (See the Browser)

```bash
npx playwright test tests/browser.spec.js --headed
```

### Run with Interactive UI

```bash
npx playwright test --ui
```

### Run Against a Different URL

```bash
TEST_URL=https://staging.dctech.events npx playwright test tests/browser.spec.js
```

---

## Setting Up Authenticated Tests

Authenticated tests require saving Cognito login sessions. This is done once (or when sessions expire) using the setup script.

### Step 1: Set Environment Variables

You'll need credentials for up to two accounts:

```bash
# Normal user credentials
export TEST_USER_EMAIL="your-regular-user@example.com"
export TEST_USER_PASSWORD="your-regular-password"

# Admin user credentials (optional)
export TEST_ADMIN_EMAIL="your-admin-user@example.com"
export TEST_ADMIN_PASSWORD="your-admin-password"
```

**Note:** You can set just one pair of credentials if you only need to test one user type.

### Step 2: Run the Setup Script

```bash
node tests/global-setup.js
```

This will:

1. Open a browser window
2. Navigate to the Cognito login page
3. Wait for you to complete login (if needed)
4. Save the authentication state to `tests/auth/user.json` and/or `tests/auth/admin.json`

**What to expect:**

```
Authenticating normal user...
✅ Normal user authentication saved to tests/auth/user.json
Authenticating admin user...
✅ Admin user authentication saved to tests/auth/admin.json
```

### Step 3: Verify Auth Files Exist

```bash
ls -la tests/auth/
```

You should see:
```
user.json   # Normal user session
admin.json  # Admin user session (if configured)
```

### Security Note

The `tests/auth/` directory is **gitignored** to prevent accidentally committing credentials. Each developer needs to run the setup with their own accounts.

---

## Running Authenticated Tests

Once auth files are set up, you can run authenticated tests.

### Normal User Tests

```bash
npx playwright test tests/authenticated.spec.js
```

Tests included:
- ✅ Access settings page
- ✅ View privacy options
- ✅ Header shows logout/submit links
- ✅ Event submission form
- ✅ Event type toggle (external/native)
- ✅ RSVP options for native events
- ✅ Recurrence picker
- ✅ Topic following
- ✅ Personalized feed
- ✅ Upvoting events

### Admin User Tests

```bash
npx playwright test tests/admin.spec.js
```

Tests included:
- ✅ Access topics API
- ✅ Create new topics (admin-only)
- ✅ Create events
- ✅ View groups
- 🔮 Moderation panel (Phase 8 - pending)

### Run All Tests

```bash
npx playwright test
```

---

## Test Coverage by Phase

### Phase 1-2: Core Navigation & Topics
| Test | File | Status |
|------|------|--------|
| Homepage loads | `browser.spec.js` | ✅ |
| Topics index page | `browser.spec.js` | ✅ |
| Groups page | `browser.spec.js` | ✅ |
| Locations pages (DC/VA/MD) | `browser.spec.js` | ✅ |
| Week view | `browser.spec.js` | ✅ |

### Phase 3: User Profiles & Authentication
| Test | File | Status |
|------|------|--------|
| Login redirects to Cognito | `browser.spec.js` | ✅ |
| Invalid profile shows not found | `browser.spec.js` | ✅ |
| Settings requires auth | `browser.spec.js` | ✅ |
| Settings page works when logged in | `authenticated.spec.js` | ✅ |
| Logout link visible | `authenticated.spec.js` | ✅ |

### Phase 4: Event Upvoting
| Test | File | Status |
|------|------|--------|
| Events display on homepage | `browser.spec.js` | ✅ |
| Upvote API works for auth users | `authenticated.spec.js` | ✅ |

### Phase 5: Native Events & RSVP
| Test | File | Status |
|------|------|--------|
| Submit form accessible | `authenticated.spec.js` | ✅ |
| Event type toggle (external/native) | `authenticated.spec.js` | ✅ |
| RSVP options appear for native | `authenticated.spec.js` | ✅ |

### Phase 6: Event Recurrence
| Test | File | Status |
|------|------|--------|
| Recurrence picker in optional details | `authenticated.spec.js` | ✅ |

### Phase 7: Discussion Boards
| Test | File | Status |
|------|------|--------|
| Threads API responds | `browser.spec.js` | ✅ |
| Thread not found handled | `browser.spec.js` | ✅ |

### Phase 8: Moderation (Future)
| Test | File | Status |
|------|------|--------|
| Admin moderation panel | `admin.spec.js` | 🔮 Skipped |
| View pending flags | `admin.spec.js` | 🔮 Skipped |

---

## CI/CD Integration

### GitHub Actions Example

Create `.github/workflows/tests.yml`:

```yaml
name: Browser Tests

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '20'
          
      - name: Install dependencies
        run: npm ci
        
      - name: Install Playwright browsers
        run: npx playwright install --with-deps
        
      - name: Run public tests
        run: npx playwright test tests/browser.spec.js
        env:
          CI: true
          TEST_URL: https://next.dctech.events
          
      - name: Upload test results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: playwright-report
          path: playwright-report/
```

### Running Authenticated Tests in CI

For authenticated tests, store credentials as GitHub secrets:

```yaml
- name: Setup authentication
  run: node tests/global-setup.js
  env:
    TEST_USER_EMAIL: ${{ secrets.TEST_USER_EMAIL }}
    TEST_USER_PASSWORD: ${{ secrets.TEST_USER_PASSWORD }}
    
- name: Run authenticated tests
  run: npx playwright test tests/authenticated.spec.js
  env:
    CI: true
```

**Security:** Use GitHub Secrets for credentials, never hardcode them.

---

## Troubleshooting

### "Auth file not found"

```
⚠️  Auth file not found at tests/auth/user.json
```

**Solution:** Run the setup script first:
```bash
node tests/global-setup.js
```

### Cognito Login Timeout

If the browser closes before you complete login:

1. Edit `tests/global-setup.js`
2. Increase the timeout values (e.g., from `30000` to `60000`)
3. Re-run the setup

### Tests Fail on CI but Pass Locally

Common causes:
- **Different timezone:** Use ISO dates in tests
- **Network speed:** Increase timeouts
- **Auth files missing:** CI needs to run setup or skip auth tests

### "Element not found"

The page may have changed. Debug with:
```bash
npx playwright test --debug
```

This opens an interactive debugger.

### View Test Report

After running tests:
```bash
npx playwright show-report
```

Opens an HTML report with screenshots and traces.

---

## Writing New Tests

### Adding a Public Test

Edit `tests/browser.spec.js`:

```javascript
test('my new public test', async ({ page }) => {
  await page.goto(BASE_URL + '/some-page');
  
  await expect(page.locator('h1')).toContainText('Expected Text');
});
```

### Adding an Authenticated Test

Edit `tests/authenticated.spec.js`:

```javascript
test('my new authenticated test', async ({ page }) => {
  // Already logged in via storage state!
  await page.goto(`${BASE_URL}/protected-page`);
  
  await expect(page.locator('body')).toContainText('Welcome');
});
```

### Adding an Admin Test

Edit `tests/admin.spec.js`:

```javascript
test('my new admin test', async ({ page }) => {
  // Already logged in as admin!
  const response = await page.request.post(`${BASE_URL}/api/admin/action`);
  
  expect(response.status()).toBe(200);
});
```

### Best Practices

1. **Use meaningful test names:** `test('user can submit an event', ...)`
2. **Handle missing data gracefully:** Use `test.skip()` if prerequisites aren't met
3. **Avoid hardcoded waits:** Use `waitForLoadState()` or `expect().toBeVisible()`
4. **Clean up test data:** If your test creates data, consider cleanup

---

## Additional Resources

- [Playwright Documentation](https://playwright.dev/docs/intro)
- [Playwright Test API](https://playwright.dev/docs/api/class-test)
- [Authentication Best Practices](https://playwright.dev/docs/auth)
- [CI/CD Setup](https://playwright.dev/docs/ci)
