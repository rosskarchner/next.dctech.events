# DC Tech Events - Browser Test Suite

This directory contains Playwright browser tests for [next.dctech.events](https://next.dctech.events).

## Setup

```bash
# Install Playwright
npm install -D @playwright/test

# Install browsers
npx playwright install
```

## Running Tests

### 1. Public/Unauthenticated Tests Only

```bash
npx playwright test tests/browser.spec.js
```

### 2. Set Up Authentication

Before running authenticated tests, you need to create the auth state files:

```bash
# Set environment variables with your test credentials
export TEST_USER_EMAIL="your-test-user@example.com"
export TEST_USER_PASSWORD="your-test-password"
export TEST_ADMIN_EMAIL="your-admin@example.com"
export TEST_ADMIN_PASSWORD="your-admin-password"

# Run the setup (opens browser for interactive login)
node tests/global-setup.js
```

This creates:
- `tests/auth/user.json` - Normal user authentication state
- `tests/auth/admin.json` - Admin user authentication state

### 3. Run Authenticated Tests

```bash
# Normal user tests
npx playwright test tests/authenticated.spec.js

# Admin tests
npx playwright test tests/admin.spec.js
```

### 4. Run All Tests

```bash
npx playwright test
```

### 5. Run with UI (Interactive Mode)

```bash
npx playwright test --ui
```

## Test Structure

| File | Description |
|------|-------------|
| `browser.spec.js` | Public/unauthenticated tests - navigation, API health, responsive |
| `authenticated.spec.js` | Normal user tests - settings, event submission, following, upvoting |
| `admin.spec.js` | Admin user tests - topic creation, moderation (future) |
| `global-setup.js` | Authentication setup script |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TEST_URL` | Base URL to test (default: `https://next.dctech.events`) |
| `TEST_USER_EMAIL` | Normal user email for authentication |
| `TEST_USER_PASSWORD` | Normal user password |
| `TEST_ADMIN_EMAIL` | Admin user email |
| `TEST_ADMIN_PASSWORD` | Admin user password |

## Test Coverage by Phase

### Phase 1-2: Core Navigation
- ✅ Homepage loads
- ✅ Topics/Groups/Locations pages
- ✅ Week view

### Phase 3: User Profiles
- ✅ Login redirect
- ✅ Profile not found handling
- ✅ Settings page (authenticated)

### Phase 4: Upvoting
- ✅ Event display
- ✅ Upvote API (authenticated)

### Phase 5: Event Submission
- ✅ Submit form structure
- ✅ Event type toggle
- ✅ RSVP options visibility

### Phase 6: Recurrence
- ✅ Recurrence picker in form

### Phase 7: Discussion Boards
- ✅ Threads API
- ✅ Thread not found handling

## CI/CD Integration

For GitHub Actions:

```yaml
- name: Run Playwright Tests
  run: npx playwright test tests/browser.spec.js
  env:
    CI: true
    TEST_URL: https://next.dctech.events
```

For authenticated tests in CI, store credentials as secrets:

```yaml
- name: Run Authenticated Tests
  run: |
    node tests/global-setup.js
    npx playwright test tests/authenticated.spec.js
  env:
    CI: true
    TEST_USER_EMAIL: ${{ secrets.TEST_USER_EMAIL }}
    TEST_USER_PASSWORD: ${{ secrets.TEST_USER_PASSWORD }}
```

## Troubleshooting

### Auth file not found
Run the global setup script first:
```bash
node tests/global-setup.js
```

### Cognito login timeout
The global setup opens a visible browser. If login takes too long, increase the timeout in `global-setup.js`.

### Tests fail on CI
- Ensure `TEST_URL` is accessible from CI environment
- Some tests skip automatically if auth files don't exist
