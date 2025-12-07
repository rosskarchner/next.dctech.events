# DC Tech Events Community Hub - Specification

**Version:** 1.1  
**Date:** 2025-12-07

## Vision
Transform DC Tech Events from an event aggregator into a community hub — "Hacker News for local tech events."

---

## 1. User Profiles

| Field | Visibility | Notes |
|-------|------------|-------|
| `nickname` | Public | Unique, required (3-30 chars) |
| `bio` | Public | Optional (0-500 chars) |
| `links` | Public | 0-5 links |
| `avatarUrl` | Public | Optional |
| `karma` | Public | Sum of upvotes received on submitted events |
| `showRsvps` | Private | Default: true. Whether to show this user in RSVP lists |
| `followedTopics` | Private | Array of topic slugs |

**Profile Page:** `/user/{nickname}` — Bio, links, karma, submitted events.

---

## 2. Topic Following

- Users can **follow topics** from the topic page
- Followed topics stored in user profile
- **Personalized Feed:** Logged-in users with followed topics see:
  - Upcoming events in those topics (homepage sidebar or dedicated `/my-feed` page)
  - New discussion threads in followed topics

---

## 3. Event Upvoting & Featured Events

### Upvote Mechanics
- One upvote per user per event
- Users **cannot** upvote their own submissions
- Upvotes are **private** (no public list of what a user upvoted)
- Downvotes: Technical foundation exists but not exposed via UI/API

### Featured Events Block
- Top events by upvote count where `eventDate` is within next 14 days
- **Tie-breaker:** Extend the list to include all tied events
- Cached 60 seconds

---

## 4. RSVP Privacy

| Setting | Controlled By | Notes |
|---------|---------------|-------|
| `showRsvpList` | Event creator | Show names vs. only counts |
| `showRsvps` | User profile | Allow name to appear in RSVP lists |

**Display Logic:**
- If event shows RSVP list: Display users who have `showRsvps: true`
- Hidden users shown as: "and 7 more"

---

## 5. Event Creation & Recurrence

### Event Types
- **External Link:** URL to Meetup, Eventbrite, etc.
- **Native Event:** Full page with optional RSVP

### Recurrence Rules
- `WEEKLY:day` — e.g., `WEEKLY:TUE`
- `MONTHLY:ordinal:day` — e.g., `MONTHLY:3:WED` (3rd Wednesday)
- `MONTHLY:LAST:day` — e.g., `MONTHLY:LAST:THU`

Backend expands into individual instances for next 90 days.

---

## 6. Topics

| Field | Notes |
|-------|-------|
| `slug` | URL identifier |
| `name` | Display name |
| `description` | Short description |
| `color` | Hex for visual tags |

- **Topic creation:** Admin-only
- **Topic assignment:** Groups can have a topic; iCal events inherit group's topic. Individual events tagged manually.
- **Topic pages:** `/topics/{slug}` — Events + Discussion board

---

## 7. Discussion Boards (HN-style)

- Threads sorted by: Hot, New, Top
- Nested replies (max depth: 5)
- Upvotes on threads and replies
- Flagging available (see Moderation)

---

## 8. Moderation

| Feature | Notes |
|---------|-------|
| **Content Flagging** | Events and discussions can be flagged for accuracy or content |
| **Moderation Queue** | Flags + new event submissions (unless user has karma ≥ threshold) |
| **Karma Threshold** | Configurable (default: 10). Users above threshold bypass submission review |
| **Shadowban** | Spammer content visible only to themselves |

---

## 9. Authentication

- **Email/Password** via Cognito
- **Social Providers** (manually configured): Google, Apple, GitHub
- **Onboarding:** New users set nickname at `/profile/setup`

**Anonymous Access:** Unauthenticated users can view all content. Actions (upvote, RSVP, post) require login.

---

## 10. Email Notifications

| Trigger | Description |
|---------|-------------|
| **Reply to thread** | When someone replies to a thread you created |
| **New events in followed topics** | Daily/weekly digest of new events in topics you follow |
| **RSVP reminder** | 24 hours before an event you RSVP'd to |

- Sent via **Amazon SES**
- Users can configure preferences in profile settings (per-type opt-out)
- Digest frequency configurable: daily or weekly

---

## 11. Data Model Summary

| Table | PK | SK | Key GSIs |
|-------|----|----|----------|
| Users | userId | - | nicknameIndex |
| Events | eventId | - | dateIndex, topicIndex |
| EventUpvotes | eventId | userId | - |
| RSVPs | eventId | userId | userRSVPsIndex |
| Topics | slug | - | - |
| TopicFollows | userId | topicSlug | topicFollowersIndex |
| Threads | topicSlug | threadId | - |
| Replies | threadId | replyId | - |
| Flags | targetType#targetId | flagId | pendingFlagsIndex |
| Groups | groupId | - | activeGroupsIndex, topicIndex |
