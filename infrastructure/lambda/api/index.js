const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, GetCommand, PutCommand, UpdateCommand, DeleteCommand, QueryCommand, BatchWriteCommand, ScanCommand } = require('@aws-sdk/lib-dynamodb');


const { CognitoJwtVerifier } = require('aws-jwt-verify');

const { v4: uuidv4 } = require('uuid');

const Handlebars = require('handlebars');
const fs = require('fs');
const path = require('path');

const { extractLocationInfo, normalizeAddress, getRegionName } = require('./location_utils');

const client = new DynamoDBClient({});
const docClient = DynamoDBDocumentClient.from(client);

// ============================================
// In-Memory Cache for frequently accessed data
// ============================================
const cache = {
  groups: { data: null, expiry: 0 },
};
const CACHE_TTL_MS = 60 * 1000; // 60 seconds

// Helper to get user display name for denormalization
async function getUserDisplayName(userId) {
  try {
    const result = await docClient.send(new GetCommand({
      TableName: process.env.USERS_TABLE,
      Key: { userId },
      ProjectionExpression: 'fullname, email',
    }));
    if (result.Item) {
      return result.Item.fullname || result.Item.email?.split('@')[0] || userId.substring(0, 8);
    }
  } catch (e) {
    console.error('Error fetching user display name:', e);
  }
  return userId.substring(0, 8); // Fallback to truncated UUID
}

// Check if event is online-only based on location
const isOnlineOnlyEvent = (location) => {
  if (!location || typeof location !== 'string') {
    return true; // No location = online only
  }

  const locationLower = location.toLowerCase();
  const onlineIndicators = [
    'online',
    'virtual',
    'zoom',
    'webinar',
    'remote',
    'teams',
    'meet.google.com',
    'whereby.com',
    'hopin.com',
    'discord',
    'twitch'
  ];

  return onlineIndicators.some(indicator => locationLower.includes(indicator));
};

// Create JWT verifiers for Cognito tokens
const accessTokenVerifier = CognitoJwtVerifier.create({
  userPoolId: process.env.USER_POOL_ID,
  tokenUse: 'access',
  clientId: process.env.USER_POOL_CLIENT_ID,
});

const idTokenVerifier = CognitoJwtVerifier.create({
  userPoolId: process.env.USER_POOL_ID,
  tokenUse: 'id',
  clientId: process.env.USER_POOL_CLIENT_ID,
});

// ============================================
// Handlebars Template Setup
// ============================================

const TEMPLATES_PATH = '/opt/nodejs/templates';

// Template cache to avoid re-compiling
const templateCache = {};

const loadTemplate = (name) => {
  if (templateCache[name]) {
    return templateCache[name];
  }

  try {
    const templatePath = path.join(TEMPLATES_PATH, `${name}.hbs`);
    const templateString = fs.readFileSync(templatePath, 'utf8');
    templateCache[name] = Handlebars.compile(templateString);
    return templateCache[name];
  } catch (error) {
    console.error(`Failed to load template ${name}:`, error);
    return null;
  }
};

// Register partials
const registerPartials = () => {
  try {
    const partialsPath = path.join(TEMPLATES_PATH, 'partials');
    if (fs.existsSync(partialsPath)) {
      const partialFiles = fs.readdirSync(partialsPath);
      partialFiles.forEach(file => {
        const name = file.replace('.hbs', '');
        const partial = fs.readFileSync(path.join(partialsPath, file), 'utf8');
        Handlebars.registerPartial(name, partial);
      });
    }
  } catch (error) {
    console.error('Failed to register partials:', error);
  }
};

// Register custom Handlebars helpers
Handlebars.registerHelper('upper', (str) => str?.toUpperCase() || '');
Handlebars.registerHelper('formatTime', (time) => {
  if (!time) return '';
  const [h, m] = time.split(':');
  const hour = parseInt(h);
  const meridiem = hour >= 12 ? 'pm' : 'am';
  const displayHour = hour > 12 ? hour - 12 : (hour === 0 ? 12 : hour);
  return `${displayHour}:${m} ${meridiem}`;
});

Handlebars.registerHelper('formatDate', (date) => {
  if (!date) return '';
  return new Date(date).toLocaleDateString('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: 'numeric'
  });
});

Handlebars.registerHelper('formatShortDate', (date) => {
  if (!date) return '';
  return new Date(date).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric'
  });
});

Handlebars.registerHelper('eq', (a, b) => a === b);
Handlebars.registerHelper('contains', (arr, item) => arr?.includes(item) || false);
Handlebars.registerHelper('year', () => new Date().getFullYear());
Handlebars.registerHelper('getState', (location) => {
  if (!location) return '';
  const parts = location.split(',').map(p => p.trim());
  return parts.length >= 2 ? parts[1] : '';
});

// Helper to get state abbreviation from "City, STATE" format
Handlebars.registerHelper('getStateAbbrev', (cityState) => {
  if (!cityState) return '';
  const parts = cityState.split(',').map(p => p.trim());
  return parts.length >= 2 ? parts[1].toLowerCase() : '';
});

// Helper to get city slug from "City, STATE" format
Handlebars.registerHelper('getCitySlug', (cityState) => {
  if (!cityState) return '';
  const parts = cityState.split(',').map(p => p.trim());
  return parts.length >= 1 ? parts[0].toLowerCase().replace(/\s+/g, '-') : '';
});

// Helper to get substring of a string
Handlebars.registerHelper('substring', (str, start, end) => {
  if (!str) return '';
  return str.substring(start, end);
});

// Initialize partials on Lambda cold start
registerPartials();


// Template rendering helper
// Note: Templates use {{}} for automatic HTML escaping. The {{{content}}} in base.hbs
// is intentional for rendering pre-rendered page content, not user input directly.
// User inputs must be escaped before passing to templates or use {{}} syntax.
const renderTemplate = (name, data) => {
  const template = loadTemplate(name);
  if (!template) {
    return `<div>Error: Template ${name} not found</div>`;
  }
  return template(data);
};

// Helper to verify and decode JWT token with signature verification
async function verifyJWT(token) {
  // Try access token first
  try {
    const payload = await accessTokenVerifier.verify(token);
    return payload;
  } catch (error) {
    // If access token verification fails, try ID token
    try {
      const payload = await idTokenVerifier.verify(token);
      return payload;
    } catch (idError) {
      console.error('JWT verification failed:', idError.message);
      return null;
    }
  }
}

// Helper to check if user is in the 'admin' Cognito group
async function isUserAdmin(userId) {
  if (!userId) return false;

  try {
    const { CognitoIdentityProviderClient, AdminListGroupsForUserCommand } = await import('@aws-sdk/client-cognito-identity-provider');
    const cognitoClient = new CognitoIdentityProviderClient({});

    const command = new AdminListGroupsForUserCommand({
      UserPoolId: process.env.USER_POOL_ID,
      Username: userId,
    });

    const response = await cognitoClient.send(command);
    const groups = response.Groups || [];
    return groups.some(group => group.GroupName === 'admin');
  } catch (error) {
    console.error('Error checking admin status:', error);
    return false;
  }
}

// Helper to parse API Gateway event
async function parseEvent(event) {
  const path = event.path || event.resource;
  const method = event.httpMethod;
  const headers = event.headers || {};
  const isHtmx = headers['hx-request'] === 'true' || headers['HX-Request'] === 'true';

  // Parse body - could be JSON or form data
  let body = null;
  if (event.body) {
    const contentType = headers['content-type'] || headers['Content-Type'] || '';
    if (contentType.includes('application/json')) {
      body = JSON.parse(event.body);
    } else if (contentType.includes('application/x-www-form-urlencoded')) {
      // Parse form data
      body = {};
      const params = new URLSearchParams(event.body);
      for (const [key, value] of params) {
        body[key] = value;
      }
    } else {
      try {
        body = JSON.parse(event.body);
      } catch {
        body = event.body;
      }
    }
  }

  const pathParams = event.pathParameters || {};
  const queryParams = event.queryStringParameters || {};

  // Try to get user from authorizer context (if API Gateway handled it)
  let userId = event.requestContext?.authorizer?.claims?.sub || null;
  let userEmail = event.requestContext?.authorizer?.claims?.email || null;

  // If not in authorizer context, verify token from Authorization header
  if (!userId) {
    const authHeader = headers['authorization'] || headers['Authorization'] || '';
    if (authHeader.startsWith('Bearer ')) {
      const token = authHeader.substring(7);
      const claims = await verifyJWT(token);
      if (claims) {
        userId = claims.sub || null;
        userEmail = claims.email || null;
      }
    }
  }

  // If still no userId, check for idToken cookie
  if (!userId) {
    const cookieHeader = headers['cookie'] || headers['Cookie'] || '';
    const cookies = {};
    cookieHeader.split(';').forEach(cookie => {
      const [name, ...rest] = cookie.split('=');
      if (name && rest.length > 0) {
        cookies[name.trim()] = rest.join('=').trim();
      }
    });

    if (cookies.idToken) {
      const claims = await verifyJWT(cookies.idToken);
      if (claims) {
        userId = claims.sub || null;
        userEmail = claims.email || null;
      }
    }
  }

  return { path, method, body, pathParams, queryParams, userId, userEmail, isHtmx };
}

// Helper to create API response
function createResponse(statusCode, body, isHtml = false) {
  const headers = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type,Authorization,HX-Request,HX-Target,HX-Trigger',
    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
  };

  if (isHtml) {
    headers['Content-Type'] = 'text/html';
    return {
      statusCode,
      headers,
      body: body,
    };
  } else {
    headers['Content-Type'] = 'application/json';
    return {
      statusCode,
      headers,
      body: JSON.stringify(body),
    };
  }
}

// HTML escape helper
function escapeHtml(text) {
  if (!text) return '';
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// HTML rendering helpers
const html = {
  groupCard: (group) => `
    <div class="group-card">
      <h3>${escapeHtml(group.name)}</h3>
      ${group.description ? `<p>${escapeHtml(group.description)}</p>` : ''}
      ${group.website ? `
        <div class="group-website">
          <a href="${escapeHtml(group.website)}" target="_blank" rel="noopener noreferrer">Website</a>
        </div>
      ` : ''}
      <div style="margin-top: 15px;">
        <a href="/group.html?id=${group.groupId}" class="btn btn-primary">View Details</a>
      </div>
    </div>
  `,

  eventCard: (event) => {
    const date = new Date(event.eventDate);
    const dateStr = date.toLocaleDateString();
    return `
      <div class="event-card">
        <h3>${escapeHtml(event.title)}</h3>
        <div class="event-date">${dateStr}${event.time ? ` at ${event.time}` : ''}</div>
        ${event.location ? `<div class="event-location">${escapeHtml(event.location)}</div>` : ''}
        ${event.description ? `<p style="margin-top: 10px;">${escapeHtml(event.description)}</p>` : ''}
        <div style="margin-top: 15px;">
          <a href="/event.html?id=${event.eventId}" class="btn btn-primary">View Details</a>
        </div>
      </div>
    `;
  },

  memberItem: (member, currentUserId, isOwner) => `
    <div class="member-item" id="member-${member.userId}">
      <div class="member-info">
        <strong>${escapeHtml(member.userName || member.userId.substring(0, 8))}</strong>
        <span style="margin-left: 10px; color: #7f8c8d;">(${member.role})</span>
      </div>
      ${isOwner && member.userId !== currentUserId ? `
        <div class="member-actions">
          <select hx-put="/api/groups/${member.groupId}/members/${member.userId}"
                  hx-target="#member-${member.userId}"
                  hx-swap="outerHTML"
                  name="role">
            <option value="member" ${member.role === 'member' ? 'selected' : ''}>Member</option>
            <option value="manager" ${member.role === 'manager' ? 'selected' : ''}>Manager</option>
            <option value="owner" ${member.role === 'owner' ? 'selected' : ''}>Owner</option>
          </select>
          <button class="btn btn-danger btn-small"
                  hx-delete="/api/groups/${member.groupId}/members/${member.userId}"
                  hx-target="#member-${member.userId}"
                  hx-swap="outerHTML"
                  hx-confirm="Remove this member?">
            Remove
          </button>
        </div>
      ` : ''}
    </div>
  `,

  messageItem: (message) => `
    <div class="message-item">
      <div class="message-meta">${escapeHtml(message.userId)} • ${new Date(message.timestamp).toLocaleString()}</div>
      <div>${escapeHtml(message.content)}</div>
    </div>
  `,

  groupDetail: (group, isMember, isOwner) => `
    <div class="group-header">
        <h1>${escapeHtml(group.name)}</h1>
        ${group.website ? `<a href="${escapeHtml(group.website)}" target="_blank" class="group-website">Visit Website</a>` : ''}
    </div>
    
    <div class="group-content">
        <p class="description">${escapeHtml(group.description)}</p>
        
        <div class="group-actions" id="group-actions">
            ${isOwner ? `
                <a href="/edit-group.html?id=${group.groupId}" class="btn btn-secondary">Manage Group</a>
                <button class="btn btn-primary" onclick="alert('Invite feature coming soon!')">Invite Admin</button>
            ` : isMember ? `
                <button hx-delete="/api/groups/${group.groupId}/members/${group.currentUserId}" 
                        hx-target="#group-actions" 
                        hx-swap="outerHTML"
                        class="btn btn-danger">Leave Group</button>
            ` : `
                <button hx-post="/api/groups/${group.groupId}/members" 
                        hx-target="#group-actions" 
                        hx-swap="outerHTML"
                        class="btn btn-primary">Join Group</button>
            `}
            ${(isOwner || isMember) ? `
                <a href="/create-event.html?groupId=${group.groupId}" class="btn btn-success">Create Event</a>
            ` : ''}
        </div>
    </div>

    <div class="group-sections">
        <section class="events-section">
            <h2>Upcoming Events</h2>
            <div id="group-events" 
                 hx-get="/api/events?groupId=${group.groupId}" 
                 hx-trigger="load">
                <div class="htmx-indicator">Loading events...</div>
            </div>
        </section>

        <section class="members-section">
            <h2>Members</h2>
            <div id="group-members" 
                 hx-get="/api/groups/${group.groupId}/members" 
                 hx-trigger="load">
                <div class="htmx-indicator">Loading members...</div>
            </div>
        </section>
    </div>
  `,

  eventDetail: (event, rsvpStatus) => `
    <div class="event-header">
        <h1>${escapeHtml(event.title)}</h1>
        <div class="event-meta">
            <span class="date">${new Date(event.eventDate).toLocaleDateString()}</span>
            <span class="time">${event.time || ''}</span>
        </div>
        ${event.location ? `<div class="location">📍 ${escapeHtml(event.location)}</div>` : ''}
        ${event.url ? `<div class="link">🔗 <a href="${escapeHtml(event.url)}" target="_blank">Event Link</a></div>` : ''}
    </div>

    <div class="event-content">
        <p class="description">${escapeHtml(event.description)}</p>
        
        <div class="rsvp-section" id="rsvp-section">
            <h3>Your RSVP</h3>
            <div class="rsvp-buttons">
                <button hx-post="/api/events/${event.eventId}/rsvps" 
                        hx-vals='{"status": "yes"}'
                        hx-target="#rsvp-section"
                        class="btn ${rsvpStatus === 'yes' ? 'btn-success' : 'btn-outline-success'}">
                    Going
                </button>
                <button hx-post="/api/events/${event.eventId}/rsvps" 
                        hx-vals='{"status": "maybe"}'
                        hx-target="#rsvp-section"
                        class="btn ${rsvpStatus === 'maybe' ? 'btn-warning' : 'btn-outline-warning'}">
                    Maybe
                </button>
                <button hx-post="/api/events/${event.eventId}/rsvps" 
                        hx-vals='{"status": "no"}'
                        hx-target="#rsvp-section"
                        class="btn ${rsvpStatus === 'no' ? 'btn-danger' : 'btn-outline-danger'}">
                    Not Going
                </button>
            </div>
        </div>

        <section class="attendees-section">
            <h3>Who's Going</h3>
            <div id="event-rsvps" 
                 hx-get="/api/events/${event.eventId}/rsvps" 
                 hx-trigger="load">
                <div class="htmx-indicator">Loading attendees...</div>
            </div>
        </section>
    </div>
  `,

  error: (message) => `<div class="message error">${escapeHtml(message)}</div>`,
  success: (message) => `<div class="message success">${escapeHtml(message)}</div>`,
};

// Helper to check group permissions
async function checkGroupPermission(groupId, userId, requiredRole = 'member') {
  const result = await docClient.send(new GetCommand({
    TableName: process.env.GROUP_MEMBERS_TABLE,
    Key: { groupId, userId },
  }));

  if (!result.Item) {
    return { hasPermission: false, role: null };
  }

  const role = result.Item.role;
  const roleHierarchy = { owner: 3, manager: 2, member: 1 };
  const hasPermission = roleHierarchy[role] >= roleHierarchy[requiredRole];

  return { hasPermission, role };
}

// Helper to get event creator
async function isEventCreator(eventId, userId) {
  const result = await docClient.send(new GetCommand({
    TableName: process.env.EVENTS_TABLE,
    Key: { eventId },
  }));

  return result.Item && result.Item.createdBy === userId;
}

// User handlers
async function getUser(userId) {
  const result = await docClient.send(new GetCommand({
    TableName: process.env.USERS_TABLE,
    Key: { userId },
  }));

  if (!result.Item) {
    return createResponse(404, { error: 'User not found' });
  }

  return createResponse(200, result.Item);
}

// Get user by nickname (for public profile pages)
async function getUserByNickname(nickname) {
  const result = await docClient.send(new QueryCommand({
    TableName: process.env.USERS_TABLE,
    IndexName: 'nicknameIndex',
    KeyConditionExpression: 'nickname = :nickname',
    ExpressionAttributeValues: {
      ':nickname': nickname,
    },
    Limit: 1,
  }));

  return result.Items?.[0] || null;
}

// Check if nickname is available
async function checkNicknameAvailable(nickname, currentUserId = null) {
  const existingUser = await getUserByNickname(nickname);
  if (!existingUser) return true;
  // If the nickname belongs to the current user, it's "available" for them
  return existingUser.userId === currentUserId;
}

// Setup profile for new user (set nickname)
async function setupProfile(userId, nickname) {
  // Validate nickname format
  if (!nickname || nickname.length < 3 || nickname.length > 30) {
    return createResponse(400, { error: 'Nickname must be 3-30 characters' });
  }
  if (!/^[a-zA-Z0-9_-]+$/.test(nickname)) {
    return createResponse(400, { error: 'Nickname can only contain letters, numbers, underscores, and hyphens' });
  }

  // Check availability
  const available = await checkNicknameAvailable(nickname, userId);
  if (!available) {
    return createResponse(409, { error: 'Nickname already taken' });
  }

  const timestamp = new Date().toISOString();

  await docClient.send(new UpdateCommand({
    TableName: process.env.USERS_TABLE,
    Key: { userId },
    UpdateExpression: 'SET nickname = :nickname, updatedAt = :updatedAt',
    ExpressionAttributeValues: {
      ':nickname': nickname,
      ':updatedAt': timestamp,
    },
  }));

  return createResponse(200, { message: 'Profile setup complete', nickname });
}

// Get public profile data (for /user/{nickname} pages)
async function getPublicProfile(nickname, isHtmx) {
  const user = await getUserByNickname(nickname);
  if (!user) {
    // Always return HTML for page requests
    const htmlContent = renderTemplate('profile_page', {
      notFound: true,
      nickname,
    });
    return createResponse(404, htmlContent, true);
  }

  // Get user's submitted events
  const eventsResult = await docClient.send(new QueryCommand({
    TableName: process.env.EVENTS_TABLE,
    IndexName: 'dateEventsIndex',
    KeyConditionExpression: 'eventType = :eventType',
    FilterExpression: 'createdBy = :userId',
    ExpressionAttributeValues: {
      ':eventType': 'all',
      ':userId': user.userId,
    },
  }));

  const publicData = {
    nickname: user.nickname,
    bio: user.bio || '',
    links: user.links || [],
    avatarUrl: user.avatarUrl || '',
    karma: user.karma || 0,
    submittedEvents: eventsResult.Items || [],
    isAuthenticated: false, // Will be set by caller if needed
  };

  // Always return HTML for page requests
  const htmlContent = renderTemplate('profile_page', publicData);
  return createResponse(200, htmlContent, true);
}

// Update user profile
async function updateUser(userId, updates) {
  const timestamp = new Date().toISOString();

  // If updating nickname, check availability
  if (updates.nickname) {
    const available = await checkNicknameAvailable(updates.nickname, userId);
    if (!available) {
      return createResponse(409, { error: 'Nickname already taken' });
    }
  }

  // Build update expression for allowed fields
  const allowedFields = ['nickname', 'bio', 'links', 'avatarUrl', 'showRsvps', 'emailPrefs', 'followedTopics'];
  const updateParts = [];
  const expressionValues = { ':updatedAt': timestamp };
  const expressionNames = {};

  for (const field of allowedFields) {
    if (updates[field] !== undefined) {
      updateParts.push(`#${field} = :${field}`);
      expressionValues[`:${field}`] = updates[field];
      expressionNames[`#${field}`] = field;
    }
  }

  if (updateParts.length === 0) {
    return createResponse(400, { error: 'No valid fields to update' });
  }

  updateParts.push('updatedAt = :updatedAt');

  await docClient.send(new UpdateCommand({
    TableName: process.env.USERS_TABLE,
    Key: { userId },
    UpdateExpression: `SET ${updateParts.join(', ')}`,
    ExpressionAttributeValues: expressionValues,
    ExpressionAttributeNames: Object.keys(expressionNames).length > 0 ? expressionNames : undefined,
  }));

  return createResponse(200, { message: 'User updated successfully' });
}


// ============================================
// Topics handlers
// ============================================

// List all topics
async function listTopics(isHtmx, userId = null, isAdmin = false) {
  // Topics table uses slug as PK only, so we use Scan
  const result = await docClient.send(new ScanCommand({
    TableName: process.env.TOPICS_TABLE,
  }));

  const topics = result.Items || [];

  // Sort alphabetically by name
  topics.sort((a, b) => (a.name || '').localeCompare(b.name || ''));

  // Always return HTML for page requests (browser navigation)
  // isHtmx distinguishes between full page load and HTMX partial
  const htmlContent = renderTemplate('topics_index', {
    topics,
    isAuthenticated: !!userId,
    isAdmin,
  });
  return createResponse(200, htmlContent, true);
}


// Get single topic with its events
async function getTopic(slug, isHtmx, userId = null) {
  // Get topic details
  const topicResult = await docClient.send(new GetCommand({
    TableName: process.env.TOPICS_TABLE,
    Key: { slug },
  }));

  if (!topicResult.Item) {
    if (isHtmx) {
      return createResponse(404, html.error('Topic not found'), true);
    }
    return createResponse(404, { error: 'Topic not found' });
  }

  const topic = topicResult.Item;

  // Get upcoming events in this topic
  const today = formatDate(new Date());
  const eventsResult = await docClient.send(new QueryCommand({
    TableName: process.env.EVENTS_TABLE,
    IndexName: 'topicIndex',
    KeyConditionExpression: 'topicSlug = :slug AND eventDate >= :today',
    ExpressionAttributeValues: {
      ':slug': slug,
      ':today': today,
    },
    Limit: 50,
  }));

  const events = eventsResult.Items || [];

  // Check if user is following this topic
  let isFollowing = false;
  if (userId) {
    isFollowing = await isFollowingTopic(userId, slug);
  }

  if (isHtmx) {
    const eventsByDay = prepareEventsByDay(events);
    const htmlContent = renderTemplate('topic_page', {
      topic,
      events,
      eventsByDay,
      isFollowing,
      isAuthenticated: !!userId,
    });
    return createResponse(200, htmlContent, true);
  }

  return createResponse(200, { topic, events, isFollowing });
}


// Create topic (admin only - access control done in route handler)
async function createTopic(userId, data, isHtmx = false) {
  const { slug, name, description, color } = data;

  if (!slug || !name) {
    if (isHtmx) {
      return createResponse(400, html.error('Slug and name are required'), true);
    }
    return createResponse(400, { error: 'Slug and name are required' });
  }

  // Validate slug format
  if (!/^[a-z0-9-]+$/.test(slug)) {
    if (isHtmx) {
      return createResponse(400, html.error('Slug must be lowercase letters, numbers, and hyphens only'), true);
    }
    return createResponse(400, { error: 'Slug must be lowercase letters, numbers, and hyphens only' });
  }

  const timestamp = new Date().toISOString();

  try {
    await docClient.send(new PutCommand({
      TableName: process.env.TOPICS_TABLE,
      Item: {
        slug,
        name,
        description: description || '',
        color: color || '#3b82f6',
        createdAt: timestamp,
        createdBy: userId,
      },
      ConditionExpression: 'attribute_not_exists(slug)',
    }));
  } catch (err) {
    if (err.name === 'ConditionalCheckFailedException') {
      if (isHtmx) {
        return createResponse(409, html.error('A topic with this slug already exists'), true);
      }
      return createResponse(409, { error: 'Topic already exists' });
    }
    throw err;
  }

  if (isHtmx) {
    return createResponse(201, html.success(`Topic "${name}" created successfully! <a href="/topics/${slug}/">View topic →</a>`), true);
  }
  return createResponse(201, { message: 'Topic created', slug });
}

// Get events by topic
async function getEventsByTopic(topicSlug, limit = 50) {
  const today = formatDate(new Date());
  const result = await docClient.send(new QueryCommand({
    TableName: process.env.EVENTS_TABLE,
    IndexName: 'topicIndex',
    KeyConditionExpression: 'topicSlug = :slug AND eventDate >= :today',
    ExpressionAttributeValues: {
      ':slug': topicSlug,
      ':today': today,
    },
    Limit: limit,
  }));

  return result.Items || [];
}

// ============================================
// Topic Following handlers
// ============================================

// Follow a topic
async function followTopic(userId, topicSlug, isHtmx = false) {
  const timestamp = new Date().toISOString();

  await docClient.send(new PutCommand({
    TableName: process.env.TOPIC_FOLLOWS_TABLE,
    Item: {
      userId,
      topicSlug,
      followedAt: timestamp,
    },
  }));

  // Return button HTML for HTMX swap
  if (isHtmx) {
    const html = `<button class="follow-btn following"
                          hx-delete="/api/topics/${topicSlug}/follow"
                          hx-target="#follow-button-container"
                          hx-swap="innerHTML">
                      Following ✓
                  </button>`;
    return createResponse(200, html, true);
  }

  return createResponse(200, { message: 'Topic followed', topicSlug });
}

// Unfollow a topic
async function unfollowTopic(userId, topicSlug, isHtmx = false) {
  await docClient.send(new DeleteCommand({
    TableName: process.env.TOPIC_FOLLOWS_TABLE,
    Key: { userId, topicSlug },
  }));

  // Return button HTML for HTMX swap
  if (isHtmx) {
    const html = `<button class="follow-btn"
                          hx-post="/api/topics/${topicSlug}/follow"
                          hx-target="#follow-button-container"
                          hx-swap="innerHTML">
                      Follow
                  </button>`;
    return createResponse(200, html, true);
  }

  return createResponse(200, { message: 'Topic unfollowed', topicSlug });
}

// Get topics followed by a user
async function getFollowedTopics(userId) {
  const result = await docClient.send(new QueryCommand({
    TableName: process.env.TOPIC_FOLLOWS_TABLE,
    KeyConditionExpression: 'userId = :userId',
    ExpressionAttributeValues: {
      ':userId': userId,
    },
  }));

  return result.Items || [];
}

// Check if user is following a specific topic
async function isFollowingTopic(userId, topicSlug) {
  const result = await docClient.send(new GetCommand({
    TableName: process.env.TOPIC_FOLLOWS_TABLE,
    Key: { userId, topicSlug },
  }));

  return !!result.Item;
}

// Get personalized feed for user (events from followed topics)
async function getPersonalizedFeed(userId, limit = 50) {
  const followedTopics = await getFollowedTopics(userId);

  if (followedTopics.length === 0) {
    return [];
  }

  const today = formatDate(new Date());
  const allEvents = [];

  // Fetch events for each followed topic
  for (const follow of followedTopics) {
    const events = await getEventsByTopic(follow.topicSlug, 20);
    allEvents.push(...events);
  }

  // Deduplicate and sort by date
  const uniqueEvents = [...new Map(allEvents.map(e => [e.eventId, e])).values()];
  uniqueEvents.sort((a, b) => a.eventDate.localeCompare(b.eventDate));

  return uniqueEvents.slice(0, limit);
}

// Group handlers


async function listGroups(isHtmx) {
  // Check cache first
  const now = Date.now();
  let groups;
  if (cache.groups.data && cache.groups.expiry > now) {
    groups = cache.groups.data;
  } else {
    const result = await docClient.send(new QueryCommand({
      TableName: process.env.GROUPS_TABLE,
      IndexName: 'activeGroupsIndex',
      KeyConditionExpression: 'active = :active',
      ExpressionAttributeValues: {
        ':active': 'true',
      },
    }));
    groups = result.Items || [];
    cache.groups.data = groups;
    cache.groups.expiry = now + CACHE_TTL_MS;
  }

  if (isHtmx) {
    if (groups.length === 0) {
      return createResponse(200, '<div class="card">No groups found. <a href="/create-group.html">Create the first one!</a></div>', true);
    }
    const htmlContent = groups.map(group => html.groupCard(group)).join('');
    return createResponse(200, htmlContent, true);
  }

  return createResponse(200, { groups });
}

async function getGroup(groupId, userId, isHtmx) {
  const result = await docClient.send(new GetCommand({
    TableName: process.env.GROUPS_TABLE,
    Key: { groupId },
  }));

  if (!result.Item) {
    return createResponse(404, { error: 'Group not found' });
  }

  if (isHtmx) {
    let isMember = false;
    let isOwner = false;
    if (userId) {
      const membership = await checkGroupPermission(groupId, userId, 'member');
      isMember = membership.hasPermission;
      isOwner = membership.role === 'owner';
    }
    const htmlContent = html.groupDetail({ ...result.Item, currentUserId: userId }, isMember, isOwner);
    return createResponse(200, htmlContent, true);
  }

  return createResponse(200, result.Item);
}

async function createGroup(userId, userEmail, data) {
  const groupId = uuidv4();
  const timestamp = new Date().toISOString();

  const group = {
    groupId,
    name: data.name,
    website: data.website || '',
    description: data.description || '',
    topicSlug: data.topicSlug || null,
    active: 'true',
    createdBy: userId,
    createdAt: timestamp,
    updatedAt: timestamp,
  };


  // Create group
  await docClient.send(new PutCommand({
    TableName: process.env.GROUPS_TABLE,
    Item: group,
  }));

  // Add creator as owner
  await docClient.send(new PutCommand({
    TableName: process.env.GROUP_MEMBERS_TABLE,
    Item: {
      groupId,
      userId,
      role: 'owner',
      joinedAt: timestamp,
    },
  }));

  return createResponse(201, { groupId, ...group });
}

async function updateGroup(groupId, userId, data) {
  const { hasPermission } = await checkGroupPermission(groupId, userId, 'manager');

  if (!hasPermission) {
    return createResponse(403, { error: 'Insufficient permissions' });
  }

  const timestamp = new Date().toISOString();

  await docClient.send(new UpdateCommand({
    TableName: process.env.GROUPS_TABLE,
    Key: { groupId },
    UpdateExpression: 'SET #name = :name, website = :website, description = :description, updatedAt = :updatedAt',
    ExpressionAttributeNames: {
      '#name': 'name',
    },
    ExpressionAttributeValues: {
      ':name': data.name,
      ':website': data.website || '',
      ':description': data.description || '',
      ':updatedAt': timestamp,
    },
  }));

  return createResponse(200, { message: 'Group updated successfully' });
}

async function deleteGroup(groupId, userId) {
  const { hasPermission } = await checkGroupPermission(groupId, userId, 'owner');

  if (!hasPermission) {
    return createResponse(403, { error: 'Only owners can delete groups' });
  }

  // Soft delete by marking as inactive
  await docClient.send(new UpdateCommand({
    TableName: process.env.GROUPS_TABLE,
    Key: { groupId },
    UpdateExpression: 'SET active = :active',
    ExpressionAttributeValues: {
      ':active': 'false',
    },
  }));

  return createResponse(200, { message: 'Group deleted successfully' });
}

// Group member handlers
async function listGroupMembers(groupId, isHtmx, currentUserId) {
  const result = await docClient.send(new QueryCommand({
    TableName: process.env.GROUP_MEMBERS_TABLE,
    KeyConditionExpression: 'groupId = :groupId',
    ExpressionAttributeValues: {
      ':groupId': groupId,
    },
  }));

  const members = result.Items || [];

  if (isHtmx && currentUserId) {
    // Check if current user is owner
    const currentMember = members.find(m => m.userId === currentUserId);
    const isOwner = currentMember?.role === 'owner';

    const htmlContent = members.map(member =>
      html.memberItem({ ...member, groupId }, currentUserId, isOwner)
    ).join('');
    return createResponse(200, htmlContent, true);
  }

  return createResponse(200, { members });
}

async function joinGroup(groupId, userId) {
  const timestamp = new Date().toISOString();

  // Fetch user display name for denormalization
  const userName = await getUserDisplayName(userId);

  await docClient.send(new PutCommand({
    TableName: process.env.GROUP_MEMBERS_TABLE,
    Item: {
      groupId,
      userId,
      userName,
      role: 'member',
      joinedAt: timestamp,
    },
  }));

  return createResponse(200, { message: 'Joined group successfully' });
}

async function updateMemberRole(groupId, targetUserId, currentUserId, newRole) {
  const { hasPermission } = await checkGroupPermission(groupId, currentUserId, 'owner');

  if (!hasPermission) {
    return createResponse(403, { error: 'Only owners can manage member roles' });
  }

  await docClient.send(new UpdateCommand({
    TableName: process.env.GROUP_MEMBERS_TABLE,
    Key: { groupId, userId: targetUserId },
    UpdateExpression: 'SET #role = :role',
    ExpressionAttributeNames: {
      '#role': 'role',
    },
    ExpressionAttributeValues: {
      ':role': newRole,
    },
  }));

  return createResponse(200, { message: 'Member role updated successfully' });
}

async function removeMember(groupId, targetUserId, currentUserId) {
  // Users can remove themselves, or owners/managers can remove others
  if (targetUserId !== currentUserId) {
    const { hasPermission } = await checkGroupPermission(groupId, currentUserId, 'manager');

    if (!hasPermission) {
      return createResponse(403, { error: 'Insufficient permissions' });
    }
  }

  await docClient.send(new DeleteCommand({
    TableName: process.env.GROUP_MEMBERS_TABLE,
    Key: { groupId, userId: targetUserId },
  }));

  return createResponse(200, { message: 'Member removed successfully' });
}

// Message handlers
async function listMessages(groupId, userId) {
  const { hasPermission } = await checkGroupPermission(groupId, userId, 'member');

  if (!hasPermission) {
    return createResponse(403, { error: 'Must be a group member to view messages' });
  }

  const result = await docClient.send(new QueryCommand({
    TableName: process.env.MESSAGES_TABLE,
    KeyConditionExpression: 'groupId = :groupId',
    ExpressionAttributeValues: {
      ':groupId': groupId,
    },
    ScanIndexForward: false,
    Limit: 50,
  }));

  return createResponse(200, { messages: result.Items });
}

async function postMessage(groupId, userId, content) {
  const { hasPermission } = await checkGroupPermission(groupId, userId, 'member');

  if (!hasPermission) {
    return createResponse(403, { error: 'Must be a group member to post messages' });
  }

  const timestamp = new Date().toISOString();

  // Fetch user display name for denormalization
  const userName = await getUserDisplayName(userId);

  await docClient.send(new PutCommand({
    TableName: process.env.MESSAGES_TABLE,
    Item: {
      groupId,
      timestamp,
      userId,
      userName,
      content,
    },
  }));

  return createResponse(201, { message: 'Message posted successfully' });
}

// Event handlers
async function listEvents(queryParams, isHtmx) {
  const now = new Date();
  const todayStr = now.toISOString().split('T')[0]; // YYYY-MM-DD

  const result = await docClient.send(new QueryCommand({
    TableName: process.env.EVENTS_TABLE,
    IndexName: 'dateEventsIndex',
    KeyConditionExpression: 'eventType = :eventType AND eventDate >= :today',
    ExpressionAttributeValues: {
      ':eventType': 'all',
      ':today': todayStr,
    },
    ScanIndexForward: true,
  }));

  const events = result.Items || [];
  // Already sorted by DB, no need to sort again

  if (isHtmx) {
    if (events.length === 0) {
      return createResponse(200, '<div class="card">No upcoming events. <a href="/create-event.html">Create the first one!</a></div>', true);
    }
    const htmlContent = events.map(event => html.eventCard(event)).join('');
    return createResponse(200, htmlContent, true);
  }

  return createResponse(200, { events });
}

async function getEvent(eventId, userId, isHtmx) {
  const result = await docClient.send(new GetCommand({
    TableName: process.env.EVENTS_TABLE,
    Key: { eventId },
  }));

  if (!result.Item) {
    return createResponse(404, { error: 'Event not found' });
  }

  if (isHtmx) {
    let rsvpStatus = null;
    if (userId) {
      const rsvp = await docClient.send(new GetCommand({
        TableName: process.env.RSVPS_TABLE,
        Key: { eventId, userId }
      }));
      if (rsvp.Item) {
        rsvpStatus = rsvp.Item.status;
      }
    }
    const htmlContent = html.eventDetail(result.Item, rsvpStatus);
    return createResponse(200, htmlContent, true);
  }

  return createResponse(200, result.Item);
}

async function createEvent(userId, data) {
  // Check if user has permission if this is a group event
  if (data.groupId) {
    const { hasPermission } = await checkGroupPermission(data.groupId, userId, 'manager');

    if (!hasPermission) {
      return createResponse(403, { error: 'Only group managers can create group events' });
    }
  }

  const eventId = uuidv4();
  const timestamp = new Date().toISOString();

  const event = {
    eventId,
    title: data.title,
    eventDate: data.date,
    time: data.time || '',
    location: data.location || '',
    url: data.url || '',
    description: data.description || '',
    groupId: data.groupId || null,
    topicSlug: data.topicSlug || null,
    upvoteCount: 0,
    eventType: 'all',
    createdBy: userId,
    createdAt: timestamp,
    updatedAt: timestamp,
  };


  await docClient.send(new PutCommand({
    TableName: process.env.EVENTS_TABLE,
    Item: event,
  }));

  return createResponse(201, { eventId, ...event });
}

async function updateEvent(eventId, userId, data) {
  const event = await docClient.send(new GetCommand({
    TableName: process.env.EVENTS_TABLE,
    Key: { eventId },
  }));

  if (!event.Item) {
    return createResponse(404, { error: 'Event not found' });
  }

  // Check permissions: creator or group manager
  let hasPermission = event.Item.createdBy === userId;

  if (!hasPermission && event.Item.groupId) {
    const groupPermission = await checkGroupPermission(event.Item.groupId, userId, 'manager');
    hasPermission = groupPermission.hasPermission;
  }

  if (!hasPermission) {
    return createResponse(403, { error: 'Insufficient permissions to edit this event' });
  }

  const timestamp = new Date().toISOString();

  await docClient.send(new UpdateCommand({
    TableName: process.env.EVENTS_TABLE,
    Key: { eventId },
    UpdateExpression: 'SET title = :title, eventDate = :eventDate, #time = :time, #location = :location, #url = :url, description = :description, updatedAt = :updatedAt',
    ExpressionAttributeNames: {
      '#time': 'time',
      '#location': 'location',
      '#url': 'url',
    },
    ExpressionAttributeValues: {
      ':title': data.title,
      ':eventDate': data.date,
      ':time': data.time || '',
      ':location': data.location || '',
      ':url': data.url || '',
      ':description': data.description || '',
      ':updatedAt': timestamp,
    },
  }));

  return createResponse(200, { message: 'Event updated successfully' });
}

async function deleteEvent(eventId, userId) {
  const event = await docClient.send(new GetCommand({
    TableName: process.env.EVENTS_TABLE,
    Key: { eventId },
  }));

  if (!event.Item) {
    return createResponse(404, { error: 'Event not found' });
  }

  // Check permissions
  let hasPermission = event.Item.createdBy === userId;

  if (!hasPermission && event.Item.groupId) {
    const groupPermission = await checkGroupPermission(event.Item.groupId, userId, 'manager');
    hasPermission = groupPermission.hasPermission;
  }

  if (!hasPermission) {
    return createResponse(403, { error: 'Insufficient permissions to delete this event' });
  }

  await docClient.send(new DeleteCommand({
    TableName: process.env.EVENTS_TABLE,
    Key: { eventId },
  }));

  return createResponse(200, { message: 'Event deleted successfully' });
}

// RSVP handlers
async function listRSVPs(eventId, requestingUserId = null, isHtmx = false) {
  // Get all RSVPs for this event
  const result = await docClient.send(new QueryCommand({
    TableName: process.env.RSVPS_TABLE,
    KeyConditionExpression: 'eventId = :eventId',
    ExpressionAttributeValues: {
      ':eventId': eventId,
    },
  }));

  const rsvps = result.Items || [];

  // Check if requesting user is the event creator
  const isCreator = requestingUserId ? await isEventCreator(eventId, requestingUserId) : false;

  // If event creator, return all RSVPs without filtering
  if (isCreator) {
    if (isHtmx) {
      // For event creators, show all attendees with a note
      let htmlContent = '<div class="rsvp-list">';
      if (rsvps.length === 0) {
        htmlContent += '<p class="text-muted">No RSVPs yet.</p>';
      } else {
        const going = rsvps.filter(r => r.status === 'yes');
        const maybe = rsvps.filter(r => r.status === 'maybe');

        if (going.length > 0) {
          htmlContent += `<p><strong>Going (${going.length}):</strong> ${going.map(r => escapeHtml(r.userName || r.userId)).join(', ')}</p>`;
        }
        if (maybe.length > 0) {
          htmlContent += `<p><strong>Maybe (${maybe.length}):</strong> ${maybe.map(r => escapeHtml(r.userName || r.userId)).join(', ')}</p>`;
        }
        htmlContent += '<p class="text-muted" style="font-size: 0.875rem; margin-top: 0.5rem;"><em>As the event organizer, you can see all RSVPs regardless of privacy settings.</em></p>';
      }
      htmlContent += '</div>';
      return createResponse(200, htmlContent, true);
    }
    return createResponse(200, { rsvps, isCreator: true });
  }

  // For non-creators, filter RSVPs based on each user's showRsvps setting
  // We need to fetch user privacy settings
  const visibleRsvps = [];
  let hiddenCount = 0;

  for (const rsvp of rsvps) {
    // Get user's privacy setting
    const userResult = await docClient.send(new GetCommand({
      TableName: process.env.USERS_TABLE,
      Key: { userId: rsvp.userId },
      ProjectionExpression: 'showRsvps, nickname',
    }));

    const userSettings = userResult.Item || {};
    // Default to showing RSVPs if setting not explicitly set to false
    const showRsvps = userSettings.showRsvps !== false;

    if (showRsvps) {
      // Use nickname if available, otherwise use stored userName
      rsvp.displayName = userSettings.nickname || rsvp.userName || 'Anonymous';
      visibleRsvps.push(rsvp);
    } else {
      hiddenCount++;
    }
  }

  if (isHtmx) {
    let htmlContent = '<div class="rsvp-list">';
    if (rsvps.length === 0) {
      htmlContent += '<p class="text-muted">No RSVPs yet.</p>';
    } else {
      const going = visibleRsvps.filter(r => r.status === 'yes');
      const maybe = visibleRsvps.filter(r => r.status === 'maybe');
      const goingHidden = rsvps.filter(r => r.status === 'yes').length - going.length;
      const maybeHidden = rsvps.filter(r => r.status === 'maybe').length - maybe.length;

      if (going.length > 0 || goingHidden > 0) {
        let goingText = going.map(r => escapeHtml(r.displayName)).join(', ');
        if (goingHidden > 0) {
          goingText += going.length > 0 ? ` and ${goingHidden} more` : `${goingHidden} ${goingHidden === 1 ? 'person' : 'people'}`;
        }
        htmlContent += `<p><strong>Going (${going.length + goingHidden}):</strong> ${goingText}</p>`;
      }
      if (maybe.length > 0 || maybeHidden > 0) {
        let maybeText = maybe.map(r => escapeHtml(r.displayName)).join(', ');
        if (maybeHidden > 0) {
          maybeText += maybe.length > 0 ? ` and ${maybeHidden} more` : `${maybeHidden} ${maybeHidden === 1 ? 'person' : 'people'}`;
        }
        htmlContent += `<p><strong>Maybe (${maybe.length + maybeHidden}):</strong> ${maybeText}</p>`;
      }
    }
    htmlContent += '</div>';
    return createResponse(200, htmlContent, true);
  }

  return createResponse(200, {
    rsvps: visibleRsvps,
    totalCount: rsvps.length,
    hiddenCount,
    isCreator: false
  });
}

async function createOrUpdateRSVP(eventId, userId, status) {
  const timestamp = new Date().toISOString();

  await docClient.send(new PutCommand({
    TableName: process.env.RSVPS_TABLE,
    Item: {
      eventId,
      userId,
      status,
      timestamp,
    },
  }));

  return createResponse(200, { message: 'RSVP updated successfully' });
}

async function deleteRSVP(eventId, userId) {
  await docClient.send(new DeleteCommand({
    TableName: process.env.RSVPS_TABLE,
    Key: { eventId, userId },
  }));

  return createResponse(200, { message: 'RSVP deleted successfully' });
}

// Convert RSVPs to group
async function convertRSVPsToGroup(eventId, userId, groupName) {
  // Check if user created the event
  if (!(await isEventCreator(eventId, userId))) {
    return createResponse(403, { error: 'Only event creator can convert RSVPs to group' });
  }

  // Get all RSVPs
  const rsvps = await docClient.send(new QueryCommand({
    TableName: process.env.RSVPS_TABLE,
    KeyConditionExpression: 'eventId = :eventId',
    ExpressionAttributeValues: {
      ':eventId': eventId,
    },
  }));

  // Create new group
  const groupId = uuidv4();
  const timestamp = new Date().toISOString();

  await docClient.send(new PutCommand({
    TableName: process.env.GROUPS_TABLE,
    Item: {
      groupId,
      name: groupName,
      website: '',
      description: `Group created from event RSVPs`,
      active: 'true',
      createdBy: userId,
      createdAt: timestamp,
      updatedAt: timestamp,
    },
  }));

  // Collect all member items for batch write
  const ownerUserName = await getUserDisplayName(userId);
  const memberItems = [{
    PutRequest: {
      Item: {
        groupId,
        userId,
        userName: ownerUserName,
        role: 'owner',
        joinedAt: timestamp,
      },
    },
  }];

  for (const rsvp of (rsvps.Items || [])) {
    if (rsvp.userId !== userId) {
      const memberUserName = await getUserDisplayName(rsvp.userId);
      memberItems.push({
        PutRequest: {
          Item: {
            groupId,
            userId: rsvp.userId,
            userName: memberUserName,
            role: 'member',
            joinedAt: timestamp,
          },
        },
      });
    }
  }

  // Batch write in chunks of 25
  const BATCH_SIZE = 25;
  for (let i = 0; i < memberItems.length; i += BATCH_SIZE) {
    const batch = memberItems.slice(i, i + BATCH_SIZE);
    await docClient.send(new BatchWriteCommand({
      RequestItems: {
        [process.env.GROUP_MEMBERS_TABLE]: batch,
      },
    }));
  }

  return createResponse(201, { groupId, message: 'Group created successfully from RSVPs' });
}

// ============================================
// Site Detection and Routing
// ============================================

// Determine which site (organize or next) is being accessed
const determineSite = (event) => {
  const headers = event.headers || {};
  const host = headers.host || headers.Host || '';
  const customHeader = headers['x-site'] || headers['X-Site'] || '';

  if (customHeader === 'next' || host.includes('next.dctech.events')) {
    return 'next';
  }
  return 'organize';
};

// ============================================
// Helper Functions for next.dctech.events
// ============================================

// Format events by day (mirrors Flask's prepare_events_by_day)
const prepareEventsByDay = (events, addWeekLinks = false) => {
  const eventsByDay = {};

  // Filter out online-only events and normalize locations
  const filteredEvents = events.filter(event => !isOnlineOnlyEvent(event.location));

  filteredEvents.forEach(event => {
    if (event.location) {
      event.location = normalizeAddress(event.location);
      const { city, state } = extractLocationInfo(event.location);
      event.city = city;
      event.state = state;
    }
  });

  // Group duplicates by title + date + time
  const eventGroups = {};
  filteredEvents.forEach(event => {
    const key = `${event.title}|||${event.eventDate}|||${event.time || 'TBD'}`;
    if (!eventGroups[key]) {
      eventGroups[key] = [];
    }
    eventGroups[key].push(event);
  });

  // Merge duplicates
  const mergedEvents = Object.values(eventGroups).map(group => {
    if (group.length === 1) {
      return group[0];
    }

    // Multiple events with same title/date/time - merge them
    const primary = group[0];
    const alsoPublishedBy = [];

    for (let i = 1; i < group.length; i++) {
      if (group[i].groupId && group[i].groupId !== primary.groupId) {
        alsoPublishedBy.push({
          group: group[i].group || group[i].groupId,
          url: group[i].group_website || group[i].url
        });
      }
    }

    if (alsoPublishedBy.length > 0) {
      primary.also_published_by = alsoPublishedBy;
    }

    return primary;
  });

  // Add events to their respective days
  mergedEvents.forEach(event => {
    const startDate = new Date(event.eventDate + 'T00:00:00Z');
    let endDate = null;
    if (event.endDate) {
      try {
        endDate = new Date(event.endDate + 'T00:00:00Z');
      } catch (e) {
        // Invalid end date
      }
    }

    // Generate list of dates for this event
    const eventDates = [];
    if (endDate && endDate > startDate) {
      let currentDate = new Date(startDate);
      while (currentDate <= endDate) {
        eventDates.push(new Date(currentDate));
        currentDate.setUTCDate(currentDate.getUTCDate() + 1);
      }
    } else {
      eventDates.push(startDate);
    }

    // Add event to each day
    eventDates.forEach((eventDate, i) => {
      const dayKey = eventDate.toISOString().split('T')[0];
      const shortDate = formatShortDate(dayKey);

      if (!eventsByDay[dayKey]) {
        let weekUrl = null;
        if (addWeekLinks) {
          const weekId = getCurrentWeekId(); // Would need to implement week calculation
          weekUrl = `/week/${weekId}/#${dayKey}`;
        }

        eventsByDay[dayKey] = {
          date: dayKey,
          short_date: shortDate,
          week_url: weekUrl,
          time_slots: {},
          has_events: false
        };
      }

      // Format time
      let timeKey = 'TBD';
      let formattedTime = 'TBD';
      const originalTime = event.time || '';

      if (originalTime && typeof originalTime === 'string' && originalTime.includes(':')) {
        try {
          timeKey = originalTime.trim();
          formattedTime = formatTime(timeKey);
        } catch (e) {
          // Keep TBD
        }
      }

      // Create event copy for this day
      const eventCopy = { ...event };
      eventCopy.time = timeKey !== 'TBD' ? timeKey : '';
      eventCopy.formatted_time = formattedTime;

      // Add (continuing) for multi-day events
      if (i > 0) {
        eventCopy.display_title = `${event.title} (continuing)`;
      } else {
        eventCopy.display_title = event.title;
      }

      // Create time slot if it doesn't exist
      if (!eventsByDay[dayKey].time_slots[timeKey]) {
        eventsByDay[dayKey].time_slots[timeKey] = [];
      }

      eventsByDay[dayKey].time_slots[timeKey].push(eventCopy);
      eventsByDay[dayKey].has_events = true;
    });
  });

  // Convert to sorted array
  const sortedDays = Object.keys(eventsByDay).sort();
  const daysData = [];

  sortedDays.forEach(dayKey => {
    const dayData = eventsByDay[dayKey];

    // Sort time slots
    const timeSortKey = (timeStr) => {
      if (timeStr === 'TBD') return [24, 0];
      try {
        const [hour, minute] = timeStr.split(':').map(Number);
        return [hour, minute];
      } catch (e) {
        return [0, 0];
      }
    };

    const sortedTimes = Object.keys(dayData.time_slots).sort((a, b) => {
      const [hourA, minA] = timeSortKey(a);
      const [hourB, minB] = timeSortKey(b);
      if (hourA !== hourB) return hourA - hourB;
      return minA - minB;
    });

    const timeSlots = sortedTimes.map(time => ({
      time,
      events: dayData.time_slots[time].sort((a, b) =>
        (a.title || '').localeCompare(b.title || '')
      )
    }));

    daysData.push({
      date: dayData.date,
      short_date: dayData.short_date,
      week_url: dayData.week_url,
      time_slots: timeSlots,
      has_events: dayData.has_events
    });
  });

  return daysData;
};

// JavaScript utility functions (also registered as Handlebars helpers above)
const formatShortDate = (dateStr) => {
  if (!dateStr) return '';
  const date = new Date(dateStr + 'T00:00:00Z');
  return date.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC'
  });
};

const formatTime = (time) => {
  if (!time) return '';
  const [h, m] = time.split(':');
  const hour = parseInt(h);
  const meridiem = hour >= 12 ? 'pm' : 'am';
  const displayHour = hour > 12 ? hour - 12 : (hour === 0 ? 12 : hour);
  return `${displayHour}:${m} ${meridiem}`;
};

const formatDate = (date) => {
  return date.toISOString().split('T')[0]; // YYYY-MM-DD
};

const getCurrentWeekId = () => {
  const now = new Date();
  const year = now.getFullYear();
  const week = getWeekNumber(now);
  return `${year}-W${String(week).padStart(2, '0')}`;
};

const getWeekNumber = (date) => {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  const dayNum = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  return Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
};

const getDateFromISOWeek = (year, week, day) => {
  const simple = new Date(year, 0, 1 + (week - 1) * 7);
  const dow = simple.getDay();
  const isoWeekStart = simple;
  if (dow <= 4) {
    isoWeekStart.setDate(simple.getDate() - simple.getDay() + 1);
  } else {
    isoWeekStart.setDate(simple.getDate() + 8 - simple.getDay());
  }
  isoWeekStart.setDate(isoWeekStart.getDate() + (day - 1));
  return isoWeekStart;
};

const extractCityState = (location) => {
  if (!location) return ['', ''];
  const { city, state } = extractLocationInfo(location);
  return [city || '', state || ''];
};

// Get upcoming events (next N days)
const getUpcomingEvents = async (daysAhead = 90) => {
  const endDate = new Date();
  endDate.setDate(endDate.getDate() + daysAhead);

  const result = await docClient.send(new QueryCommand({
    TableName: process.env.EVENTS_TABLE,
    IndexName: 'dateEventsIndex',
    KeyConditionExpression: 'eventType = :type AND eventDate BETWEEN :start AND :end',
    ExpressionAttributeValues: {
      ':type': 'all',
      ':start': formatDate(new Date()),
      ':end': formatDate(endDate),
    },
  }));

  return result.Items || [];
};

// Get events by week
const getEventsByWeek = async (weekId) => {
  const [year, week] = weekId.split('-W');
  const start = getDateFromISOWeek(parseInt(year), parseInt(week), 1);
  const end = getDateFromISOWeek(parseInt(year), parseInt(week), 7);

  const result = await docClient.send(new QueryCommand({
    TableName: process.env.EVENTS_TABLE,
    IndexName: 'dateEventsIndex',
    KeyConditionExpression: 'eventType = :type AND eventDate BETWEEN :start AND :end',
    ExpressionAttributeValues: {
      ':type': 'all',
      ':start': formatDate(start),
      ':end': formatDate(end),
    },
  }));

  return result.Items || [];
};

// Get events by state
const getEventsByState = async (state) => {
  const events = await getUpcomingEvents();
  return events.filter(e => {
    const [, eventState] = extractCityState(e.location);
    return eventState?.toUpperCase() === state.toUpperCase();
  });
};

// Get events by city
const getEventsByCity = async (state, city) => {
  const events = await getEventsByState(state);
  return events.filter(e => {
    const [eventCity] = extractCityState(e.location);
    return eventCity?.toLowerCase() === city.toLowerCase();
  });
};

// Get active groups
const getActiveGroups = async () => {
  const result = await docClient.send(new QueryCommand({
    TableName: process.env.GROUPS_TABLE,
    IndexName: 'activeGroupsIndex',
    KeyConditionExpression: 'active = :active',
    ExpressionAttributeValues: {
      ':active': 'true',
    },
  }));

  return result.Items || [];
};

// Get locations with event counts
const getLocationsWithEventCounts = async () => {
  const events = await getUpcomingEvents();
  const locationStats = {};
  const citySet = new Set();

  // Count events by region and collect cities
  events.forEach(event => {
    const { city, state } = extractLocationInfo(event.location || '');
    if (['DC', 'VA', 'MD'].includes(state)) {
      // Add to region count
      const region = getRegionName(state);
      locationStats[region] = (locationStats[region] || 0) + 1;

      // Add to city set (skip Washington, DC since it's same as DC region)
      if (city && !(city === 'Washington' && state === 'DC')) {
        citySet.add(`${city}, ${state}`);
      }
    }
  });

  // Get city counts
  const cityStats = {};
  for (const cityState of citySet) {
    const [city, state] = cityState.split(', ');
    const cityEvents = events.filter(event => {
      const { city: eventCity, state: eventState } = extractLocationInfo(event.location || '');
      return eventCity === city && eventState === state;
    });

    if (cityEvents.length > 0) {
      const displayCity = state === 'DC' ? 'Washington' : city;
      cityStats[`${displayCity}, ${state}`] = cityEvents.length;
    }
  }

  return {
    locationStats,
    cityStats: Object.entries(cityStats)
      .sort((a, b) => b[1] - a[1])
      .reduce((obj, [key, value]) => {
        obj[key] = value;
        return obj;
      }, {})
  };
};

// Generate stats for homepage
const generateStats = (events) => {
  const upcomingCount = events.length;
  const locations = [...new Set(events.map(e => e.location).filter(Boolean))];
  const groups = [...new Set(events.map(e => e.groupId).filter(Boolean))];

  return {
    upcomingCount,
    locationCount: locations.length,
    groupCount: groups.length,
  };
};

// Generate sitemap XML
const generateSitemap = () => {
  const baseUrl = 'https://next.dctech.events';
  const today = formatDate(new Date());

  const urls = [
    { loc: '/', lastmod: today, priority: 1.0 },
    { loc: '/week/LATEST/', lastmod: today, priority: 0.9 },
    { loc: '/locations/', lastmod: today, priority: 0.8 },
    { loc: '/groups/', lastmod: today, priority: 0.8 },
  ];

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.map(url => `  <url>
    <loc>${baseUrl}${url.loc}</loc>
    <lastmod>${url.lastmod}</lastmod>
    <priority>${url.priority}</priority>
  </url>`).join('\n')}
</urlset>`;

  return xml;
};

// Cognito login URL helper
const cognitoLoginUrl = (redirectPath) => {
  const domain = process.env.COGNITO_DOMAIN;
  const region = process.env.USER_POOL_REGION || 'us-east-1';
  const clientId = process.env.USER_POOL_CLIENT_ID;

  // Construct full Cognito URL
  const cognitoUrl = `https://${domain}.auth.${region}.amazoncognito.com`;
  const redirectUri = encodeURIComponent(`https://next.dctech.events/callback`);

  return `${cognitoUrl}/login?client_id=${clientId}&response_type=code&redirect_uri=${redirectUri}&state=${encodeURIComponent(redirectPath)}`;
};

// Cognito logout URL helper
const cognitoLogoutUrl = () => {
  const domain = process.env.COGNITO_DOMAIN;
  const region = process.env.USER_POOL_REGION || 'us-east-1';
  const clientId = process.env.USER_POOL_CLIENT_ID;

  // Construct full Cognito logout URL
  const cognitoUrl = `https://${domain}.auth.${region}.amazoncognito.com`;
  // Note: logout_uri must exactly match one of the allowed "Sign out URLs" in Cognito app client settings
  // The CDK configures 'https://next.dctech.events' (no trailing slash)
  const logoutUri = encodeURIComponent(`https://next.dctech.events`);

  return `${cognitoUrl}/logout?client_id=${clientId}&logout_uri=${logoutUri}`;
};

// ============================================
// Route Handlers for next.dctech.events
// ============================================

const handleNextRequest = async (path, method, userId, isHtmx, event, parsedBody) => {
  // ============================================
  // Nickname Enforcement: Redirect users without nickname to profile setup
  // ============================================
  // Skip this check for: login, callback, profile setup itself, static assets, API routes
  const exemptPaths = [
    '/login', '/callback', '/profile/setup', '/api/',
    '/static/', '/sitemap.xml', '/newsletter.html'
  ];
  const isExemptPath = exemptPaths.some(p => path.startsWith(p) || path === p.replace('/', ''));

  if (userId && !isExemptPath) {
    // Check if user has set up their profile (has a nickname)
    const userResult = await docClient.send(new GetCommand({
      TableName: process.env.USERS_TABLE,
      Key: { userId },
    }));

    const hasNickname = userResult.Item?.nickname;

    if (!hasNickname) {
      // Redirect to profile setup, preserving intended destination
      const returnUrl = encodeURIComponent(path);
      return {
        statusCode: 302,
        headers: { 'Location': `/profile/setup?return=${returnUrl}` },
        body: '',
      };
    }
  }

  // PUBLIC ROUTES

  // GET / - Homepage with upcoming events
  if (path === '/' && method === 'GET') {
    const events = await getUpcomingEvents();
    const eventsByDay = prepareEventsByDay(events);
    const stats = generateStats(events);

    // Fetch personalized feed data for authenticated users
    let followedTopics = [];
    let feedEventsByDay = [];
    if (userId) {
      const followedTopicRecords = await getFollowedTopics(userId);
      const topicSlugs = followedTopicRecords.map(f => f.topicSlug);

      // Get topic details for display
      if (topicSlugs.length > 0) {
        for (const slug of topicSlugs) {
          const topicResult = await docClient.send(new GetCommand({
            TableName: process.env.TOPICS_TABLE,
            Key: { slug },
          }));
          if (topicResult.Item) {
            followedTopics.push(topicResult.Item);
          }
        }

        // Get personalized feed events (limited for sidebar)
        const feedEvents = await getPersonalizedFeed(userId, 10);
        feedEventsByDay = prepareEventsByDay(feedEvents);
      }
    }

    const html = renderTemplate('homepage', {
      eventsByDay,
      stats,
      isAuthenticated: !!userId,
      isHtmx,
      followedTopics,
      feedEventsByDay,
      hasFollowedTopics: followedTopics.length > 0,
    });

    return createResponse(200, html, true);
  }

  // GET /login - Redirect to Cognito login
  if ((path === '/login' || path === '/login/') && method === 'GET') {
    return {
      statusCode: 302,
      headers: { 'Location': cognitoLoginUrl('/') },
      body: '',
    };
  }

  // GET /logout - Clear session and redirect to Cognito logout
  if ((path === '/logout' || path === '/logout/') && method === 'GET') {
    return {
      statusCode: 302,
      headers: {
        'Location': cognitoLogoutUrl(),
        // Clear the auth cookie
        'Set-Cookie': 'idToken=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; Secure; SameSite=Lax',
      },
      body: '',
    };
  }

  // GET /week/:weekId - Week view
  if (path.match(/^\/week\/[\d]{4}-W\d{2}\/?$/)) {
    const weekId = path.match(/\/week\/([\d]{4}-W\d{2})/)[1];

    // Validate week number (should be 1-53)
    const weekMatch = weekId.match(/^(\d{4})-W(\d{2})$/);
    if (weekMatch) {
      const weekNum = parseInt(weekMatch[2], 10);
      if (weekNum < 1 || weekNum > 53) {
        return createResponse(400, 'Invalid week number', false);
      }
    }

    const events = await getEventsByWeek(weekId);
    const eventsByDay = prepareEventsByDay(events);

    // Calculate prev/next week
    const [year, week] = weekId.split('-W');
    const weekNum = parseInt(week);
    const yearNum = parseInt(year);

    // Calculate previous week
    let prevWeekNum, prevYear;
    if (weekNum > 1) {
      prevWeekNum = weekNum - 1;
      prevYear = yearNum;
    } else {
      // Get last week of previous year (could be 52 or 53)
      const lastDayOfPrevYear = new Date(yearNum - 1, 11, 31);
      prevWeekNum = getWeekNumber(lastDayOfPrevYear);
      prevYear = yearNum - 1;
    }

    // Calculate next week
    let nextWeekNum, nextYear;
    // Get last week of current year to handle 52/53 week years
    const lastDayOfYear = new Date(yearNum, 11, 31);
    const maxWeeksInYear = getWeekNumber(lastDayOfYear);

    if (weekNum < maxWeeksInYear) {
      nextWeekNum = weekNum + 1;
      nextYear = yearNum;
    } else {
      nextWeekNum = 1;
      nextYear = yearNum + 1;
    }

    const prevWeek = `${prevYear}-W${String(prevWeekNum).padStart(2, '0')}`;
    const nextWeek = `${nextYear}-W${String(nextWeekNum).padStart(2, '0')}`;

    const html = renderTemplate('week_page', {
      weekId,
      eventsByDay,
      isAuthenticated: !!userId,
      currentWeek: getCurrentWeekId(),
      prevWeek,
      nextWeek,
    });

    return createResponse(200, html, true);
  }

  // GET /locations/ - Location index
  if (path === '/locations/' && method === 'GET') {
    const { locationStats, cityStats } = await getLocationsWithEventCounts();

    const html = renderTemplate('locations_index', {
      locationStats,
      cityStats,
      isAuthenticated: !!userId,
    });

    return createResponse(200, html, true);
  }

  // GET /locations/:state - State-filtered events
  if (path.match(/^\/locations\/[A-Za-z]{2}\/?$/i)) {
    const state = path.match(/\/locations\/([A-Za-z]{2})/i)[1].toUpperCase();
    const events = await getEventsByState(state);
    const eventsByDay = prepareEventsByDay(events);
    const cities = [...new Set(events.map(e => extractCityState(e.location)[0]).filter(Boolean))];

    const html = renderTemplate('location_page', {
      state,
      eventsByDay,
      cities,
      isAuthenticated: !!userId,
    });

    return createResponse(200, html, true);
  }

  // GET /locations/:state/:city - City-filtered events
  if (path.match(/^\/locations\/[A-Za-z]{2}\/[\w-]+\/?$/i)) {
    const match = path.match(/\/locations\/([A-Za-z]{2})\/([^/]+)/i);
    const state = match[1].toUpperCase();
    const citySlug = match[2];
    // Convert slug back to city name (e.g., "mc-lean" -> "McLean")
    const city = citySlug.split('-').map(word =>
      word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()
    ).join(' ');

    const events = await getEventsByCity(state, city);
    const eventsByDay = prepareEventsByDay(events);

    const html = renderTemplate('location_page', {
      state,
      city,
      eventsByDay,
      isAuthenticated: !!userId,
    });

    return createResponse(200, html, true);
  }

  // GET /groups/ - Groups list
  if (path === '/groups/' && method === 'GET') {
    const groups = await getActiveGroups();
    const sortedGroups = groups.sort((a, b) => a.name.localeCompare(b.name));

    const html = renderTemplate('groups_list', {
      groups: sortedGroups,
      isAuthenticated: !!userId,
    });

    return createResponse(200, html, true);
  }

  // ============================================
  // Topics Routes
  // ============================================

  // GET /topics/ - Topics list
  if ((path === '/topics/' || path === '/topics') && method === 'GET') {
    const isAdmin = await isUserAdmin(userId);
    return await listTopics(isHtmx, userId, isAdmin);
  }

  // GET /topics/{slug} - Single topic page
  if (path.match(/^\/topics\/[a-z0-9-]+\/?$/) && method === 'GET') {
    const slug = path.match(/^\/topics\/([a-z0-9-]+)/)[1];
    return await getTopic(slug, isHtmx, userId);
  }


  // POST /api/topics - Create topic (admin only)
  if (path === '/api/topics' && method === 'POST') {
    if (!userId) {
      return createResponse(403, { error: 'Authentication required' });
    }
    const isAdmin = await isUserAdmin(userId);
    if (!isAdmin) {
      return createResponse(403, { error: 'Admin access required' });
    }
    const body = parsedBody || {};
    return await createTopic(userId, body, isHtmx);
  }

  // POST /api/topics/{slug}/follow - Follow a topic
  if (path.match(/^\/api\/topics\/[a-z0-9-]+\/follow$/) && method === 'POST') {
    if (!userId) {
      return createResponse(403, { error: 'Authentication required' });
    }
    const slug = path.match(/^\/api\/topics\/([a-z0-9-]+)\/follow$/)[1];
    return await followTopic(userId, slug, isHtmx);
  }

  // DELETE /api/topics/{slug}/follow - Unfollow a topic
  if (path.match(/^\/api\/topics\/[a-z0-9-]+\/follow$/) && method === 'DELETE') {
    if (!userId) {
      return createResponse(403, { error: 'Authentication required' });
    }
    const slug = path.match(/^\/api\/topics\/([a-z0-9-]+)\/follow$/)[1];
    return await unfollowTopic(userId, slug, isHtmx);
  }

  // GET /my-feed - Personalized feed page
  if ((path === '/my-feed' || path === '/my-feed/') && method === 'GET') {
    if (!userId) {
      return {
        statusCode: 302,
        headers: { 'Location': cognitoLoginUrl('/my-feed') },
        body: '',
      };
    }

    const events = await getPersonalizedFeed(userId);
    const eventsByDay = prepareEventsByDay(events);
    const followedTopics = await getFollowedTopics(userId);

    // Get topic details for display
    const topicSlugs = followedTopics.map(f => f.topicSlug);
    let topicDetails = [];
    if (topicSlugs.length > 0) {
      for (const slug of topicSlugs) {
        const topicResult = await docClient.send(new GetCommand({
          TableName: process.env.TOPICS_TABLE,
          Key: { slug },
        }));
        if (topicResult.Item) {
          topicDetails.push(topicResult.Item);
        }
      }
    }

    const htmlContent = renderTemplate('my_feed', {
      eventsByDay,
      followedTopics: topicDetails,
      hasFollowedTopics: topicDetails.length > 0,
      isAuthenticated: true,
    });

    return createResponse(200, htmlContent, true);
  }

  // GET /newsletter.html - HTML newsletter
  if (path === '/newsletter.html' && method === 'GET') {
    const events = await getUpcomingEvents(14);
    const eventsByDay = prepareEventsByDay(events);

    const html = renderTemplate('newsletter', {
      format: 'html',
      eventsByDay,
    });

    return createResponse(200, html, true);
  }

  // GET /sitemap.xml - XML sitemap
  if (path === '/sitemap.xml' && method === 'GET') {
    const sitemap = generateSitemap();
    return {
      statusCode: 200,
      headers: {
        'Content-Type': 'application/xml',
      },
      body: sitemap,
    };
  }

  // GET /callback - OAuth callback handler
  if (path === '/callback' && method === 'GET') {
    const code = event.queryStringParameters?.code;
    const state = event.queryStringParameters?.state || '/';

    if (!code) {
      return createResponse(400, 'Missing authorization code', false);
    }

    // Exchange code for tokens with Cognito
    try {
      const domain = process.env.COGNITO_DOMAIN;
      const region = process.env.USER_POOL_REGION || 'us-east-1';
      const clientId = process.env.USER_POOL_CLIENT_ID;
      const tokenUrl = `https://${domain}.auth.${region}.amazoncognito.com/oauth2/token`;

      const response = await fetch(tokenUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: new URLSearchParams({
          grant_type: 'authorization_code',
          client_id: clientId,
          code: code,
          redirect_uri: 'https://next.dctech.events/callback',
        }).toString(),
      });

      const tokens = await response.json();

      if (!response.ok) {
        console.error('Token exchange failed:', tokens);
        return createResponse(400, 'Failed to authenticate', false);
      }

      // Set cookie with ID token and redirect to original destination
      return {
        statusCode: 302,
        headers: {
          'Location': state,
          'Set-Cookie': `idToken=${tokens.id_token}; Path=/; Secure; HttpOnly; Max-Age=3600`,
        },
        body: '',
      };
    } catch (error) {
      console.error('Callback error:', error);
      return createResponse(500, 'Authentication error', false);
    }
  }

  // PROTECTED ROUTES (require authentication)

  // GET /submit/ - Event submission form
  if (path === '/submit/' && method === 'GET') {
    if (!userId) {
      return {
        statusCode: 302,
        headers: {
          'Location': cognitoLoginUrl('/submit/'),
        },
        body: '',
      };
    }

    // Fetch topics for dropdown
    const topicsResult = await docClient.send(new ScanCommand({
      TableName: process.env.TOPICS_TABLE,
    }));
    const topics = (topicsResult.Items || []).sort((a, b) => (a.name || '').localeCompare(b.name || ''));

    const htmlContent = renderTemplate('submit_event', {
      userId,
      isAuthenticated: true,
      topics,
    });

    return createResponse(200, htmlContent, true);
  }


  // POST /submit/ - Create event
  if (path === '/submit/' && method === 'POST') {
    if (!userId) {
      return createResponse(403, { error: 'Authentication required' });
    }

    // Use parsedBody parameter (already parsed by parseEvent in main handler)
    // parseEvent handles both JSON and form-encoded content types
    const body = parsedBody || {};

    // Validate required fields
    if (!body.title || !body.title.trim()) {
      return createResponse(400, html.error('Event title is required'), true);
    }
    if (!body.url || !body.url.trim()) {
      return createResponse(400, html.error('Event URL is required'), true);
    }
    if (!body.date || !body.date.trim()) {
      return createResponse(400, html.error('Event date is required'), true);
    }
    if (!body.location || !body.location.trim()) {
      return createResponse(400, html.error('Event location is required'), true);
    }

    const eventId = uuidv4();
    const timestamp = new Date().toISOString();

    await docClient.send(new PutCommand({
      TableName: process.env.EVENTS_TABLE,
      Item: {
        eventId,
        title: body.title.trim(),
        eventDate: body.date,
        endDate: body.end_date || null,
        time: body.time || '',
        location: body.location || '',
        url: body.url || '',
        description: body.description || '',
        cost: body.cost || '',
        topicSlug: body.topicSlug || null,
        upvoteCount: 0,
        eventType: 'all',
        createdBy: userId,
        createdAt: timestamp,
        updatedAt: timestamp,
      },
    }));


    const html = renderTemplate('event_created_confirmation', {
      eventId,
      title: body.title,
    });

    return createResponse(200, html, true);
  }

  // GET /submit-group/ - Group submission form
  if (path === '/submit-group/' && method === 'GET') {
    if (!userId) {
      return {
        statusCode: 302,
        headers: {
          'Location': cognitoLoginUrl('/submit-group/'),
        },
        body: '',
      };
    }

    // Fetch topics for dropdown
    const topicsResult = await docClient.send(new ScanCommand({
      TableName: process.env.TOPICS_TABLE,
    }));
    const topics = (topicsResult.Items || []).sort((a, b) => (a.name || '').localeCompare(b.name || ''));

    const htmlContent = renderTemplate('submit_group', {
      userId,
      isAuthenticated: true,
      topics,
    });

    return createResponse(200, htmlContent, true);
  }


  // POST /submit-group/ - Create group
  if (path === '/submit-group/' && method === 'POST') {
    if (!userId) {
      return createResponse(403, { error: 'Authentication required' });
    }

    // Use parsedBody parameter (already parsed by parseEvent in main handler)
    // parseEvent handles both JSON and form-encoded content types
    const body = parsedBody || {};

    // Validate required fields
    if (!body.name || !body.name.trim()) {
      return createResponse(400, html.error('Group name is required'), true);
    }

    const groupId = uuidv4();
    const timestamp = new Date().toISOString();

    await docClient.send(new PutCommand({
      TableName: process.env.GROUPS_TABLE,
      Item: {
        groupId,
        name: body.name.trim(),
        website: body.website || '',
        ical: body.ical || '',
        description: body.description || '',
        active: 'false', // Requires admin approval
        createdBy: userId,
        createdAt: timestamp,
        updatedAt: timestamp,
      },
    }));

    const html = renderTemplate('group_created_confirmation', {
      groupId,
      name: body.name,
    });

    return createResponse(200, html, true);
  }

  // ============================================
  // Profile Routes
  // ============================================

  // GET /user/{nickname} - Public profile page
  if (path.match(/^\/user\/[a-zA-Z0-9_-]+\/?$/) && method === 'GET') {
    const nickname = path.match(/^\/user\/([a-zA-Z0-9_-]+)/)[1];
    return await getPublicProfile(nickname, isHtmx);
  }

  // GET /profile/setup - Profile setup page for new users
  if (path === '/profile/setup' && method === 'GET') {
    if (!userId) {
      return {
        statusCode: 302,
        headers: { 'Location': cognitoLoginUrl('/profile/setup') },
        body: '',
      };
    }

    // Check if user already has a nickname
    const userResult = await docClient.send(new GetCommand({
      TableName: process.env.USERS_TABLE,
      Key: { userId },
    }));

    if (userResult.Item?.nickname) {
      // Already set up, redirect to intended destination or profile
      const returnUrl = event.queryStringParameters?.return || `/user/${userResult.Item.nickname}`;
      return {
        statusCode: 302,
        headers: { 'Location': decodeURIComponent(returnUrl) },
        body: '',
      };
    }

    // Get return URL from query params
    const returnUrl = event.queryStringParameters?.return || '/';

    const htmlContent = renderTemplate('profile_setup', {
      isAuthenticated: true,
      returnUrl,
    });
    return createResponse(200, htmlContent, true);
  }

  // POST /api/users/setup - Save nickname for new user
  if (path === '/api/users/setup' && method === 'POST') {
    if (!userId) {
      return createResponse(403, { error: 'Authentication required' });
    }

    const body = parsedBody || {};
    return await setupProfile(userId, body.nickname);
  }

  // GET /settings - User settings page
  if (path === '/settings' && method === 'GET') {
    if (!userId) {
      return {
        statusCode: 302,
        headers: { 'Location': cognitoLoginUrl('/settings') },
        body: '',
      };
    }

    const userResult = await docClient.send(new GetCommand({
      TableName: process.env.USERS_TABLE,
      Key: { userId },
    }));

    const htmlContent = renderTemplate('settings', {
      isAuthenticated: true,
      user: userResult.Item || {},
    });
    return createResponse(200, htmlContent, true);
  }

  // PUT /api/users/me - Update current user's profile
  if (path === '/api/users/me' && method === 'PUT') {
    if (!userId) {
      return createResponse(403, { error: 'Authentication required' });
    }

    const body = parsedBody || {};
    return await updateUser(userId, body);
  }

  // GET /api/users/me - Get current user's profile
  if (path === '/api/users/me' && method === 'GET') {
    if (!userId) {
      return createResponse(403, { error: 'Authentication required' });
    }
    return await getUser(userId);
  }

  // Default 404
  return createResponse(404, '<h1>Page Not Found</h1>', true);
};


// Main handler
// Helper to check if a route requires authentication
function requiresAuth(path, method) {
  // Public routes (no auth required)
  const publicRoutes = [
    { pattern: /^\/users\/[^/]+$/, methods: ['GET'] },
    { pattern: /^\/groups$/, methods: ['GET'] },
    { pattern: /^\/groups\/[^/]+$/, methods: ['GET'] },
    { pattern: /^\/groups\/[^/]+\/members$/, methods: ['GET'] },
    { pattern: /^\/events$/, methods: ['GET'] },
    { pattern: /^\/events\/[^/]+$/, methods: ['GET'] },
    { pattern: /^\/events\/[^/]+\/rsvps$/, methods: ['GET'] },
  ];

  for (const route of publicRoutes) {
    if (route.pattern.test(path) && route.methods.includes(method)) {
      return false;
    }
  }

  // All other routes require authentication
  return true;
}

exports.handler = async (event) => {
  let isHtmx = false;
  try {
    const site = determineSite(event);
    const { path, method, body, pathParams, queryParams, userId, userEmail, isHtmx: isHtmxRequest } = await parseEvent(event);
    isHtmx = isHtmxRequest;

    // Handle CORS preflight
    if (method === 'OPTIONS') {
      return createResponse(200, {});
    }

    // Route to appropriate site handler
    if (site === 'next') {
      return await handleNextRequest(path, method, userId, isHtmx, event, body);
    }

    // Original organize.dctech.events routes below
    // Check authentication for protected routes
    if (requiresAuth(path, method) && !userId) {
      return createResponse(401, isHtmx ? html.error('Authentication required') : { error: 'Authentication required' }, isHtmx);
    }

    // User routes
    if (path === '/users' && method === 'GET') {
      return await getUser(userId);
    }
    if (path === '/users' && method === 'PUT') {
      return await updateUser(userId, body);
    }
    if (path.startsWith('/users/') && method === 'GET') {
      return await getUser(pathParams.userId);
    }

    // Group routes
    if (path === '/groups' && method === 'GET') {
      return await listGroups(isHtmx);
    }
    if (path === '/groups' && method === 'POST') {
      return await createGroup(userId, userEmail, body);
    }
    if (path.match(/^\/groups\/[^/]+$/) && method === 'GET') {
      return await getGroup(pathParams.groupId, userId, isHtmx);
    }
    if (path.match(/^\/groups\/[^/]+$/) && method === 'PUT') {
      return await updateGroup(pathParams.groupId, userId, body);
    }
    if (path.match(/^\/groups\/[^/]+$/) && method === 'DELETE') {
      return await deleteGroup(pathParams.groupId, userId);
    }

    // Group member routes
    if (path.match(/^\/groups\/[^/]+\/members$/) && method === 'GET') {
      return await listGroupMembers(pathParams.groupId, isHtmx, userId);
    }
    if (path.match(/^\/groups\/[^/]+\/members$/) && method === 'POST') {
      return await joinGroup(pathParams.groupId, userId);
    }
    if (path.match(/^\/groups\/[^/]+\/members\/[^/]+$/) && method === 'PUT') {
      return await updateMemberRole(pathParams.groupId, pathParams.userId, userId, body.role);
    }
    if (path.match(/^\/groups\/[^/]+\/members\/[^/]+$/) && method === 'DELETE') {
      return await removeMember(pathParams.groupId, pathParams.userId, userId);
    }

    // Message routes
    if (path.match(/^\/groups\/[^/]+\/messages$/) && method === 'GET') {
      return await listMessages(pathParams.groupId, userId);
    }
    if (path.match(/^\/groups\/[^/]+\/messages$/) && method === 'POST') {
      return await postMessage(pathParams.groupId, userId, body.content);
    }

    // Event routes
    if (path === '/events' && method === 'GET') {
      return await listEvents(queryParams, isHtmx);
    }
    if (path === '/events' && method === 'POST') {
      return await createEvent(userId, body);
    }
    if (path.match(/^\/events\/[^/]+$/) && method === 'GET') {
      return await getEvent(pathParams.eventId, userId, isHtmx);
    }
    if (path.match(/^\/events\/[^/]+$/) && method === 'PUT') {
      return await updateEvent(pathParams.eventId, userId, body);
    }
    if (path.match(/^\/events\/[^/]+$/) && method === 'DELETE') {
      return await deleteEvent(pathParams.eventId, userId);
    }

    // RSVP routes
    if (path.match(/^\/events\/[^/]+\/rsvps$/) && method === 'GET') {
      return await listRSVPs(pathParams.eventId, userId, isHtmx);
    }
    if (path.match(/^\/events\/[^/]+\/rsvps$/) && method === 'POST') {
      return await createOrUpdateRSVP(pathParams.eventId, userId, body.status);
    }
    if (path.match(/^\/events\/[^/]+\/rsvps$/) && method === 'DELETE') {
      return await deleteRSVP(pathParams.eventId, userId);
    }

    // Convert RSVPs to group
    if (path.match(/^\/events\/[^/]+\/convert-to-group$/) && method === 'POST') {
      return await convertRSVPsToGroup(pathParams.eventId, userId, body.groupName);
    }

    return createResponse(404, { error: 'Not found' });
  } catch (error) {
    console.error('Error:', error);
    if (isHtmx) {
      return createResponse(500, html.error(error.message), true);
    }
    return createResponse(500, { error: error.message });
  }
};
