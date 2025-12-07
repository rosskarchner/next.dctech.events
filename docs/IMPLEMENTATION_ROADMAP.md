# Community Hub Transformation Roadmap

Phased plan to transform DC Tech Events into the community hub described in `COMMUNITY_HUB_SPEC.md`.

---

## Existing Infrastructure Audit

### Current DynamoDB Tables (Keep All)
| Table | Status | Changes Needed |
|-------|--------|----------------|
| `organize-users` | **KEEP** | Add: `nickname`, `bio`, `links[]`, `avatarUrl`, `karma`, `showRsvps`, `emailPrefs`, `followedTopics[]` |
| `organize-groups` | **KEEP** | Add: `topicSlug` field |
| `organize-group-members` | **KEEP** | Already has `userName` (from prior optimization) |
| `organize-events` | **KEEP** | Add: `upvoteCount`, `rsvpEnabled`, `rsvpLimit`, `showRsvpList`, `recurrenceRule`, `parentEventId`, `topicSlugs[]`, `status` (pending/approved) |
| `organize-rsvps` | **KEEP** | No changes |
| `organize-messages` | **DEPRECATE** | Replace with Topics-based discussion boards |

### New DynamoDB Tables (To Create)
| Table | PK | SK | Purpose |
|-------|----|----|---------|
| `Topics` | slug | - | Topic definitions |
| `TopicFollows` | userId | topicSlug | User → Topic subscriptions |
| `EventUpvotes` | eventId | userId | Event upvote tracking |
| `Threads` | topicSlug | threadId | Discussion threads per topic |
| `Replies` | threadId | replyId | Thread replies |
| `Flags` | targetType#targetId | flagId | Content moderation flags |

### Current API Functions (in `lambda/api/index.js`)
| Function | Status | Notes |
|----------|--------|-------|
| `getUser`, `updateUser` | **MODIFY** | Add new profile fields |
| `listGroups`, `getGroup`, `createGroup`, `updateGroup`, `deleteGroup` | **KEEP** | Add topic assignment |
| `listGroupMembers`, `joinGroup`, `updateMemberRole`, `removeMember` | **KEEP** | No changes |
| `listMessages`, `postMessage` | **DEPRECATE** | Group messaging → Topic discussions |
| `listEvents`, `getEvent`, `createEvent`, `updateEvent`, `deleteEvent` | **MODIFY** | Add upvotes, recurrence, moderation status |
| `listRSVPs`, `createOrUpdateRSVP`, `deleteRSVP` | **MODIFY** | Add privacy filtering |
| `convertRSVPsToGroup` | **LOW PRIORITY** | Keep but rarely used |
| `handleNextRequest` | **EXTEND** | Add new routes for profiles, topics, discussions |

### Current Templates (in `lambda/layers/templates/nodejs/templates/`)
| Template | Status | Notes |
|----------|--------|-------|
| `homepage.hbs` | **MODIFY** | Add featured events block, personalized feed section |
| `location_page.hbs` | **KEEP** | No changes |
| `locations_index.hbs` | **KEEP** | No changes |
| `groups_list.hbs` | **MODIFY** | Add topic badges |
| `submit_event.hbs` | **MODIFY** | Add topic picker, recurrence picker, RSVP options |
| `submit_group.hbs` | **MODIFY** | Add topic picker |
| `week_page.hbs` | **KEEP** | No changes |
| `partials/header.hbs` | **MODIFY** | Add logged-in user state, profile link |
| `partials/event_card.hbs` | **MODIFY** | Add upvote button, topic badges |
| `partials/events_by_day.hbs` | **KEEP** | No changes |

### New Templates (To Create)
- `profile_setup.hbs` — Nickname setup for new users
- `profile_page.hbs` — Public profile view
- `settings.hbs` — User settings
- `topic_page.hbs` — Topic with events + discussions
- `topics_index.hbs` — All topics listing
- `my_feed.hbs` — Personalized feed
- `thread_page.hbs` — Discussion thread with replies
- `event_page.hbs` — Native event detail (extended from current)
- `admin/moderation.hbs` — Moderation queue

### Files/Features to Deprecate
| Item | Reason | Migration |
|------|--------|-----------|
| `organize-messages` table | Replaced by topic-based discussions | No user data in production |
| `listMessages`, `postMessage` APIs | Same | Remove routes |
| Group-based messaging UI | Never fully implemented | N/A |

---

## Phase 1: Foundation (User Profiles)

### 1.1 Extend User Model
**CDK Changes (`infrastructure-stack.ts`):**
- [ ] Add GSI to Users table: `nicknameIndex` (PK: `nickname`)

**API Changes (`lambda/api/index.js`):**
- [ ] Modify `updateUser()` to handle new fields: `nickname`, `bio`, `links[]`, `avatarUrl`, `showRsvps`
- [ ] Add `getUserByNickname()` helper function
- [ ] Add nickname uniqueness check

**New Routes:**
- [ ] `GET /user/{nickname}` → public profile page
- [ ] `GET /profile/setup` → nickname setup page (new users)
- [ ] `POST /api/users/setup` → save nickname
- [ ] `GET /settings` → user settings page
- [ ] `PUT /api/users/me` → update profile

**New Templates:**
- [ ] Create `profile_setup.hbs`
- [ ] Create `profile_page.hbs`
- [ ] Create `settings.hbs`

**Modify Templates:**
- [ ] Update `partials/header.hbs` — show logged-in user, link to profile

---

## Phase 2: Topics Infrastructure

### 2.1 Topics Table
**CDK Changes:**
- [ ] Create `Topics` table (PK: `slug`)

**API Changes:**
- [ ] Add `listTopics()`, `getTopic()`, `createTopic()` (admin-only)
- [ ] Add topic routes to `handleNextRequest()`

**New Routes:**
- [ ] `GET /topics` → topics index
- [ ] `GET /topics/{slug}` → topic page
- [ ] `POST /api/topics` → create topic (admin)

**New Templates:**
- [ ] Create `topics_index.hbs`
- [ ] Create `topic_page.hbs` (events list + discussions placeholder)

### 2.2 Assign Topics to Content
**CDK Changes:**
- [ ] Add GSI to Groups: `topicIndex` (PK: `topicSlug`)
- [ ] Add GSI to Events: `topicIndex` (PK: `topicSlug`, SK: `eventDate`)

**API Changes:**
- [ ] Modify `createGroup()`, `updateGroup()` to accept `topicSlug`
- [ ] Modify `createEvent()` to accept `topicSlugs[]`
- [ ] Add `getEventsByTopic()` helper

**Modify Templates:**
- [ ] Update `submit_group.hbs` — add topic dropdown
- [ ] Update `submit_event.hbs` — add topic multi-select
- [ ] Update `partials/event_card.hbs` — display topic badges

---

## Phase 3: Topic Following & Personalized Feed

### 3.1 Follow/Unfollow
**CDK Changes:**
- [x] Create `TopicFollows` table (PK: `userId`, SK: `topicSlug`)
- [x] Add GSI: `topicFollowersIndex` (PK: `topicSlug`)

**API Changes:**
- [x] Add `followTopic()`, `unfollowTopic()`, `getFollowedTopics()`
- [x] Add routes: `POST/DELETE /api/topics/{slug}/follow`

**Modify Templates:**
- [x] Update `topic_page.hbs` — add Follow/Unfollow button

### 3.2 Personalized Feed
**New Routes:**
- [x] `GET /my-feed` → personalized feed page

**New Templates:**
- [x] Create `my_feed.hbs`

**Modify Templates:**
- [x] Update `homepage.hbs` — add "Your Feed" sidebar for logged-in users

---

## Phase 4: Event Upvoting & Featured Events ✅

### 4.1 Upvotes Infrastructure
**CDK Changes:**
- [x] Create `EventUpvotes` table (PK: `eventId`, SK: `userId`)

**API Changes:**
- [x] Add `upvoteEvent()`, `removeUpvote()`, `getUpvoteStatus()`
- [x] Modify `createEvent()` to initialize `upvoteCount: 0`
- [x] Add karma increment/decrement logic

**New Routes:**
- [x] `POST /api/events/{id}/upvote`
- [x] `DELETE /api/events/{id}/upvote`

### 4.2 Featured Events Block
**API Changes:**
- [x] Add `getFeaturedEvents()` — top by upvoteCount, next 14 days, extend ties
- [ ] Add caching (60 sec) — *deferred*

**Modify Templates:**
- [ ] Update `homepage.hbs` — add Featured Events block above calendar — *deferred*
- [ ] Update `partials/event_card.hbs` — add upvote button with count — *deferred*

---

## Phase 5: Native Events & RSVP Enhancements ✅

### 5.1 Event Type & RSVP Fields
**API Changes:**
- [x] Modify Events schema: add `isNative`, `rsvpEnabled`, `rsvpLimit`, `showRsvpList`
- [x] Modify `createEvent()` for new fields
- [x] Unified event submission form with radio toggle for external vs native events

### 5.2 RSVP Privacy
**API Changes:**
- [x] Modify `listRSVPs()` to filter by user's `showRsvps` setting
- [x] Event creators always see all RSVPs regardless of privacy settings
- [x] Return visible count + hidden count ("and X more")

**Modify Templates:**
- [x] Create `event_detail.hbs` for native event pages with RSVP support
- [x] Update `submit_event.hbs` with unified form and RSVP privacy settings

---

## Phase 6: Event Recurrence

### 6.1 Recurrence Model
**API Changes:**
- [ ] Add `recurrenceRule` and `parentEventId` to Events
- [ ] Create `expandRecurringEvent()` helper

**New Lambda:**
- [ ] Create `lambda/recurrence/index.js` — daily expansion job

**CDK Changes:**
- [ ] Add EventBridge rule for daily recurrence expansion

**Modify Templates:**
- [ ] Update `submit_event.hbs` — add recurrence picker

---

## Phase 7: Discussion Boards

### 7.1 Tables
**CDK Changes:**
- [ ] Create `Threads` table (PK: `topicSlug`, SK: `threadId`)
- [ ] Create `Replies` table (PK: `threadId`, SK: `replyId`)

### 7.2 APIs
**API Changes:**
- [ ] Add `listThreads()`, `getThread()`, `createThread()`
- [ ] Add `createReply()`, `getReplies()`
- [ ] Add upvote helpers for threads/replies

**New Routes:**
- [ ] `GET /api/topics/{slug}/threads`
- [ ] `POST /api/topics/{slug}/threads`
- [ ] `GET /threads/{id}`
- [ ] `POST /api/threads/{id}/replies`

**New Templates:**
- [ ] Update `topic_page.hbs` — add discussions section
- [ ] Create `thread_page.hbs`

### 7.3 Deprecate Group Messages
- [ ] Remove `listMessages`, `postMessage` routes
- [ ] Remove from `handleNextRequest()` routing

---

## Phase 8: Moderation

### 8.1 Flags Table
**CDK Changes:**
- [ ] Create `Flags` table (PK: `targetType#targetId`, SK: `flagId`)
- [ ] Add GSI: `pendingFlagsIndex`

### 8.2 APIs
**API Changes:**
- [ ] Add `createFlag()`, `listPendingFlags()`, `resolveFlag()`
- [ ] Add `status` field to Events (pending/approved)
- [ ] Modify `createEvent()` to check karma for auto-approval

**New Routes:**
- [ ] `POST /api/flags`
- [ ] `GET /admin/moderation`
- [ ] `POST /api/admin/flags/{id}/resolve`
- [ ] `POST /api/admin/users/{id}/shadowban`

**New Templates:**
- [ ] Create `admin/moderation.hbs`

---

## Phase 9: Email Notifications

### 9.1 SES Setup
**CDK Changes:**
- [ ] Create SES email identity (verify domain)
- [ ] Add SES permissions to Lambda

### 9.2 Notification Preferences
**API Changes:**
- [ ] Add `emailPrefs` to User schema
- [ ] Update settings page

### 9.3 Trigger Lambdas
**New Lambdas:**
- [ ] `lambda/notifications/reply.js` — triggered on new reply
- [ ] `lambda/notifications/digest.js` — daily/weekly digest
- [ ] `lambda/notifications/reminder.js` — RSVP reminders (24h before)

**CDK Changes:**
- [ ] EventBridge rules for digest and reminder Lambdas

---

## Phase 10: Polish & Launch

### 10.1 UI/UX
- [ ] Audit all templates use consistent `{{> header}}` and `{{> footer}}`
- [ ] Mobile responsiveness review
- [ ] Add loading states for HTMX interactions

### 10.2 Performance
- [ ] Review all caching (groups, featured events, topics)
- [ ] Add CloudWatch dashboards for new Lambdas

### 10.3 Cleanup
- [ ] Remove deprecated `organize-messages` table (after confirming empty)
- [ ] Remove `listMessages`, `postMessage` from codebase
- [ ] Update README and DEPLOYMENT.md

### 10.4 Launch
- [ ] Deploy to production
- [ ] Seed initial topics
- [ ] Announce to community
