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
// Input Validation Functions
// ============================================

// Validate UUID format (accepts v4 UUIDs)
function validateUUID(id, paramName = 'ID') {
  if (!id) {
    throw new Error(`${paramName} is required`);
  }
  // Strict UUID v4 format: version must be 4, variant must be 8/9/a/b
  const uuidV4Regex = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  if (!uuidV4Regex.test(id)) {
    throw new Error(`Invalid ${paramName} format`);
  }
  return id;
}

// Validate slug format (lowercase alphanumeric with hyphens)
function validateSlug(slug, paramName = 'slug') {
  if (!slug) {
    throw new Error(`${paramName} is required`);
  }
  const slugRegex = /^[a-z0-9-]{1,100}$/;
  if (!slugRegex.test(slug)) {
    throw new Error(`Invalid ${paramName} format. Use lowercase letters, numbers, and hyphens only.`);
  }
  return slug;
}

// Validate nickname format
function validateNickname(nickname) {
  if (!nickname) {
    throw new Error('Nickname is required');
  }
  if (nickname.length < 3 || nickname.length > 30) {
    throw new Error('Nickname must be 3-30 characters');
  }
  const nicknameRegex = /^[a-zA-Z0-9_-]+$/;
  if (!nicknameRegex.test(nickname)) {
    throw new Error('Nickname can only contain letters, numbers, underscores, and hyphens');
  }
  return nickname;
}

// Validate email format
function validateEmail(email) {
  if (!email) {
    throw new Error('Email is required');
  }
  const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  if (!emailRegex.test(email)) {
    throw new Error('Invalid email format');
  }
  return email;
}

// Validate URL format and prevent SSRF
function validateURL(url, paramName = 'URL') {
  if (!url) {
    throw new Error(`${paramName} is required`);
  }
  try {
    const parsed = new URL(url);
    if (!['http:', 'https:'].includes(parsed.protocol)) {
      throw new Error(`${paramName} must use HTTP or HTTPS protocol`);
    }
    
    // Check for SSRF vulnerabilities - block private IPs and cloud metadata
    const hostname = parsed.hostname.toLowerCase();
    
    // Block common dangerous patterns
    const dangerousPatterns = [
      /^localhost$/i,
      /^127\./,
      /^10\./,
      /^172\.(1[6-9]|2[0-9]|3[01])\./,
      /^192\.168\./,
      /^169\.254\./,  // AWS/Azure metadata service
      /^::1$/,
      /^fe80:/i,
      /^fc00:/i,
      /^fd[0-9a-f]{2}:/i,
      /^::ffff:127\./i, // IPv4-mapped IPv6 loopback
      /^::ffff:10\./i,  // IPv4-mapped IPv6 private
      /^::ffff:192\.168\./i,
      /^::ffff:172\.(1[6-9]|2[0-9]|3[01])\./i,
      /^::ffff:169\.254\./i,
      /metadata\.google\.internal/i, // GCP metadata
      /^169\.254\.169\.254$/,  // AWS metadata explicit
    ];
    
    if (dangerousPatterns.some(pattern => pattern.test(hostname))) {
      throw new Error(`${paramName} cannot point to private/internal/metadata addresses`);
    }
    
    // Additional check: if hostname looks like an IP, validate it's not private
    const ipv4Regex = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;
    const ipMatch = hostname.match(ipv4Regex);
    if (ipMatch) {
      const octets = ipMatch.slice(1, 5).map(Number);
      // Check if it's a valid IP and not in private ranges
      if (octets.some(octet => octet > 255)) {
        throw new Error(`Invalid ${paramName}: Invalid IP address`);
      }
      // Additional private range checks
      if (octets[0] === 0 || octets[0] === 127 || octets[0] === 10 ||
          (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
          (octets[0] === 192 && octets[1] === 168) ||
          (octets[0] === 169 && octets[1] === 254)) {
        throw new Error(`${paramName} cannot point to private addresses`);
      }
    }
    
    return url;
  } catch (error) {
    throw new Error(`Invalid ${paramName}: ${error.message}`);
  }
}

// Validate string length
function validateString(str, minLen, maxLen, paramName = 'Field') {
  if (!str || typeof str !== 'string') {
    throw new Error(`${paramName} is required`);
  }
  if (str.length < minLen || str.length > maxLen) {
    throw new Error(`${paramName} must be between ${minLen} and ${maxLen} characters`);
  }
  return str.trim();
}

// SECURITY NOTE: HTML Sanitization Strategy
// This application uses escapeHtml() for all user-generated content to prevent XSS.
// Templates use {{variable}} syntax which auto-escapes HTML.
// If rich HTML content is ever needed, install and use the 'dompurify' or 'sanitize-html' npm package.
// Do NOT attempt to write custom regex-based HTML sanitizers as they are prone to bypasses.

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

// Format relative time (e.g., "2 hours ago", "3 days ago")
function formatRelativeTime(dateString) {
  const date = new Date(dateString);
  const now = new Date();
  const diffMs = now - date;
  const diffSecs = Math.floor(diffMs / 1000);
  const diffMins = Math.floor(diffSecs / 60);
  const diffHours = Math.floor(diffMins / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffDays > 30) {
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  } else if (diffDays > 0) {
    return `${diffDays} day${diffDays !== 1 ? 's' : ''} ago`;
  } else if (diffHours > 0) {
    return `${diffHours} hour${diffHours !== 1 ? 's' : ''} ago`;
  } else if (diffMins > 0) {
    return `${diffMins} min${diffMins !== 1 ? 's' : ''} ago`;
  } else {
    return 'just now';
  }
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

// Helper to get allowed origin for CORS
function getAllowedOrigin(requestHeaders = {}) {
  const allowedOrigins = [
    'https://next.dctech.events',
    'https://organize.dctech.events',
    process.env.NODE_ENV === 'development' ? 'http://localhost:3000' : null,
  ].filter(Boolean);

  const origin = requestHeaders['origin'] || requestHeaders['Origin'] || '';
  // If origin is in allowed list, return it; otherwise return first allowed origin
  return allowedOrigins.includes(origin) ? origin : allowedOrigins[0];
}

// Helper to create API response
// requestHeaders parameter is optional - if not provided, will use default origin
function createResponse(statusCode, body, isHtml = false, requestHeaders = {}) {
  const headers = {
    'Access-Control-Allow-Origin': getAllowedOrigin(requestHeaders),
    'Access-Control-Allow-Headers': 'Content-Type,Authorization,HX-Request,HX-Target,HX-Trigger',
    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
    'Access-Control-Allow-Credentials': 'true',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'X-XSS-Protection': '1; mode=block',
    'Strict-Transport-Security': 'max-age=31536000; includeSubDomains; preload',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
  };

  if (isHtml) {
    headers['Content-Type'] = 'text/html; charset=utf-8';
    // CSP with unsafe-inline is necessary for htmx inline attributes
    // TODO: Consider migrating to htmx with external scripts and using nonces
    headers['Content-Security-Policy'] = [
      "default-src 'self'",
      "script-src 'self' 'unsafe-inline' https://unpkg.com",  // htmx uses inline scripts
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data: https:",
      "connect-src 'self' https://*.amazoncognito.com https://*.amazonaws.com",
      "frame-ancestors 'none'",
      "base-uri 'self'",
      "form-action 'self'",
    ].join('; ');
    return {
      statusCode,
      headers,
      body: body,
    };
  } else {
    headers['Content-Type'] = 'application/json; charset=utf-8';
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
    if (isHtmx) {
      return createResponse(404, html.error('Event not found'), true);
    }
    return createResponse(404, { error: 'Event not found' });
  }

  const event = result.Item;

  // For native events (or HTMX requests), return full detail page
  if (event.isNative || isHtmx) {
    // Get user's RSVP status
    let userRsvp = null;
    if (userId) {
      const rsvpResult = await docClient.send(new GetCommand({
        TableName: process.env.RSVPS_TABLE,
        Key: { eventId, userId }
      }));
      userRsvp = rsvpResult.Item?.status || null;
    }

    // Get upvote status
    const { hasUpvoted } = await getUpvoteStatus(eventId, userId);

    // Get topic info if available
    let topic = null;
    if (event.topicSlug) {
      const topicResult = await docClient.send(new GetCommand({
        TableName: process.env.TOPICS_TABLE,
        Key: { slug: event.topicSlug },
      }));
      topic = topicResult.Item || null;
    }

    // Get creator nickname
    let creatorNickname = null;
    if (event.createdBy) {
      const creatorResult = await docClient.send(new GetCommand({
        TableName: process.env.USERS_TABLE,
        Key: { userId: event.createdBy },
      }));
      creatorNickname = creatorResult.Item?.nickname || null;
    }

    // Get RSVP count
    let rsvpCount = 0;
    if (event.rsvpEnabled) {
      const rsvpsResult = await docClient.send(new QueryCommand({
        TableName: process.env.RSVPS_TABLE,
        KeyConditionExpression: 'eventId = :eventId',
        ExpressionAttributeValues: { ':eventId': eventId },
        Select: 'COUNT',
      }));
      rsvpCount = rsvpsResult.Count || 0;
    }

    const htmlContent = renderTemplate('event_detail', {
      ...event,
      topic,
      isAuthenticated: !!userId,
      hasUpvoted,
      userRsvp,
      userRsvpYes: userRsvp === 'yes',
      userRsvpMaybe: userRsvp === 'maybe',
      creatorNickname,
      rsvpCount,
    });
    return createResponse(200, htmlContent, true);
  }

  // For external link events, return JSON (API use)
  return createResponse(200, event);
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

  // Determine if this is a native event (hosted on dctech.events) or external link
  const isNative = data.eventType === 'native' || data.isNative === true || data.isNative === 'true';

  const event = {
    eventId,
    title: data.title,
    eventDate: data.date,
    endDate: data.end_date || null,
    time: data.time || '',
    location: data.location || '',
    url: isNative ? null : (data.url || ''),
    description: data.description || '',
    groupId: data.groupId || null,
    topicSlug: data.topicSlug || null,
    cost: data.cost || null,
    upvoteCount: 0,
    eventType: 'all', // For GSI queries
    isNative,
    // Recurrence (Phase 6)
    recurrenceRule: data.recurrenceRule || null,
    // RSVP settings (only meaningful for native events)
    rsvpEnabled: isNative && (data.rsvpEnabled === true || data.rsvpEnabled === 'true' || data.rsvpEnabled === 'on'),
    rsvpLimit: isNative && data.rsvpLimit ? parseInt(data.rsvpLimit, 10) : null,
    showRsvpList: isNative ? (data.showRsvpList !== 'false' && data.showRsvpList !== false) : true, // Default to showing
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

// ============================================
// Event Upvote Functions (Phase 4)
// ============================================

// Upvote an event
async function upvoteEvent(eventId, userId, isHtmx = false) {
  // Get the event to check it exists and user isn't the creator
  const eventResult = await docClient.send(new GetCommand({
    TableName: process.env.EVENTS_TABLE,
    Key: { eventId },
  }));

  if (!eventResult.Item) {
    if (isHtmx) {
      return createResponse(404, html.error('Event not found'), true);
    }
    return createResponse(404, { error: 'Event not found' });
  }

  // Users cannot upvote their own events
  if (eventResult.Item.createdBy === userId) {
    if (isHtmx) {
      return createResponse(403, html.error('You cannot upvote your own event'), true);
    }
    return createResponse(403, { error: 'Cannot upvote your own event' });
  }

  const timestamp = new Date().toISOString();

  try {
    // Add upvote record (will fail if already exists due to PK+SK)
    await docClient.send(new PutCommand({
      TableName: process.env.EVENT_UPVOTES_TABLE,
      Item: {
        eventId,
        userId,
        createdAt: timestamp,
      },
      ConditionExpression: 'attribute_not_exists(eventId) AND attribute_not_exists(userId)',
    }));

    // Increment upvote count on the event
    await docClient.send(new UpdateCommand({
      TableName: process.env.EVENTS_TABLE,
      Key: { eventId },
      UpdateExpression: 'SET upvoteCount = if_not_exists(upvoteCount, :zero) + :inc',
      ExpressionAttributeValues: {
        ':inc': 1,
        ':zero': 0,
      },
    }));

    // Increment karma for event creator
    if (eventResult.Item.createdBy) {
      await docClient.send(new UpdateCommand({
        TableName: process.env.USERS_TABLE,
        Key: { userId: eventResult.Item.createdBy },
        UpdateExpression: 'SET karma = if_not_exists(karma, :zero) + :inc',
        ExpressionAttributeValues: {
          ':inc': 1,
          ':zero': 0,
        },
      }));
    }

    const newCount = (eventResult.Item.upvoteCount || 0) + 1;

    if (isHtmx) {
      // Return updated upvote button showing "upvoted" state
      return createResponse(200, `
        <button class="upvote-btn upvoted" 
                hx-delete="/api/events/${eventId}/upvote" 
                hx-target="this" 
                hx-swap="outerHTML">
          ▲ ${newCount}
        </button>
      `, true);
    }

    return createResponse(200, { message: 'Upvoted', upvoteCount: newCount });
  } catch (err) {
    if (err.name === 'ConditionalCheckFailedException') {
      // Already upvoted
      if (isHtmx) {
        return createResponse(200, html.error('Already upvoted'), true);
      }
      return createResponse(409, { error: 'Already upvoted' });
    }
    throw err;
  }
}

// Remove upvote from an event
async function removeUpvote(eventId, userId, isHtmx = false) {
  // Check if upvote exists
  const upvoteResult = await docClient.send(new GetCommand({
    TableName: process.env.EVENT_UPVOTES_TABLE,
    Key: { eventId, userId },
  }));

  if (!upvoteResult.Item) {
    if (isHtmx) {
      return createResponse(404, html.error('No upvote to remove'), true);
    }
    return createResponse(404, { error: 'No upvote to remove' });
  }

  // Get current event upvote count
  const eventResult = await docClient.send(new GetCommand({
    TableName: process.env.EVENTS_TABLE,
    Key: { eventId },
  }));

  // Remove upvote record
  await docClient.send(new DeleteCommand({
    TableName: process.env.EVENT_UPVOTES_TABLE,
    Key: { eventId, userId },
  }));

  // Decrement upvote count on the event
  await docClient.send(new UpdateCommand({
    TableName: process.env.EVENTS_TABLE,
    Key: { eventId },
    UpdateExpression: 'SET upvoteCount = upvoteCount - :dec',
    ExpressionAttributeValues: {
      ':dec': 1,
    },
  }));

  // Decrement karma for event creator
  if (eventResult.Item?.createdBy) {
    await docClient.send(new UpdateCommand({
      TableName: process.env.USERS_TABLE,
      Key: { userId: eventResult.Item.createdBy },
      UpdateExpression: 'SET karma = karma - :dec',
      ExpressionAttributeValues: {
        ':dec': 1,
      },
    }));
  }

  const newCount = Math.max(0, (eventResult.Item?.upvoteCount || 1) - 1);

  if (isHtmx) {
    // Return updated upvote button showing "not upvoted" state
    return createResponse(200, `
      <button class="upvote-btn" 
              hx-post="/api/events/${eventId}/upvote" 
              hx-target="this" 
              hx-swap="outerHTML">
        ▲ ${newCount}
      </button>
    `, true);
  }

  return createResponse(200, { message: 'Upvote removed', upvoteCount: newCount });
}

// Check if user has upvoted an event
async function getUpvoteStatus(eventId, userId) {
  if (!userId) {
    return { hasUpvoted: false };
  }

  const result = await docClient.send(new GetCommand({
    TableName: process.env.EVENT_UPVOTES_TABLE,
    Key: { eventId, userId },
  }));

  return { hasUpvoted: !!result.Item };
}

// Get featured events (top by upvotes in the next 14 days)
async function getFeaturedEvents(limit = 5) {
  const today = formatDate(new Date());
  const twoWeeksFromNow = new Date();
  twoWeeksFromNow.setDate(twoWeeksFromNow.getDate() + 14);
  const futureDate = formatDate(twoWeeksFromNow);

  // Get upcoming events
  const result = await docClient.send(new QueryCommand({
    TableName: process.env.EVENTS_TABLE,
    IndexName: 'dateEventsIndex',
    KeyConditionExpression: 'eventType = :eventType AND eventDate BETWEEN :today AND :future',
    ExpressionAttributeValues: {
      ':eventType': 'all',
      ':today': today,
      ':future': futureDate,
    },
  }));

  const events = result.Items || [];

  // Sort by upvote count (descending), then by date
  events.sort((a, b) => {
    const upvoteDiff = (b.upvoteCount || 0) - (a.upvoteCount || 0);
    if (upvoteDiff !== 0) return upvoteDiff;
    return a.eventDate.localeCompare(b.eventDate);
  });

  // Get top events, but extend to include ties
  if (events.length <= limit) {
    return events;
  }

  const featured = events.slice(0, limit);
  const cutoffScore = featured[featured.length - 1]?.upvoteCount || 0;

  // Include any events tied with the last one
  for (let i = limit; i < events.length; i++) {
    if ((events[i].upvoteCount || 0) === cutoffScore) {
      featured.push(events[i]);
    } else {
      break;
    }
  }

  return featured;
}

// ============================================
// Phase 7: Discussion Board Functions
// ============================================

// Calculate HN-style "hot" score for sorting
function calculateHotScore(upvotes, createdAt) {
  const age = (Date.now() - new Date(createdAt).getTime()) / (1000 * 60 * 60); // Hours
  return (upvotes || 0) / Math.pow(age + 2, 1.8);
}

// List threads for a topic
async function listThreads(topicSlug, sort = 'new', isHtmx = false) {
  let result;

  if (sort === 'new') {
    result = await docClient.send(new QueryCommand({
      TableName: process.env.THREADS_TABLE,
      IndexName: 'threadsByDateIndex',
      KeyConditionExpression: 'topicSlug = :slug',
      ExpressionAttributeValues: { ':slug': topicSlug },
      ScanIndexForward: false, // Newest first
      Limit: 50,
    }));
  } else {
    // For 'hot' and 'top', we need to get all and sort in memory
    result = await docClient.send(new QueryCommand({
      TableName: process.env.THREADS_TABLE,
      KeyConditionExpression: 'topicSlug = :slug',
      ExpressionAttributeValues: { ':slug': topicSlug },
    }));
  }

  let threads = result.Items || [];

  // Apply sorting
  if (sort === 'top') {
    threads.sort((a, b) => (b.upvoteCount || 0) - (a.upvoteCount || 0));
  } else if (sort === 'hot') {
    threads = threads.map(t => ({ ...t, hotScore: calculateHotScore(t.upvoteCount, t.createdAt) }));
    threads.sort((a, b) => b.hotScore - a.hotScore);
  }

  // Enrich with author nicknames
  for (const thread of threads) {
    if (thread.authorId) {
      const author = await docClient.send(new GetCommand({
        TableName: process.env.USERS_TABLE,
        Key: { userId: thread.authorId },
      }));
      thread.authorNickname = author.Item?.nickname || 'Anonymous';
    }
  }

  if (isHtmx) {
    const threadsHtml = threads.map(t => `
      <div class="thread-item">
        <div class="thread-votes">▲ ${t.upvoteCount || 0}</div>
        <div class="thread-content">
          <a href="/threads/${t.threadId}" class="thread-title">${escapeHtml(t.title)}</a>
          <div class="thread-meta">
            by <a href="/user/${t.authorNickname}">${escapeHtml(t.authorNickname)}</a>
            · ${t.replyCount || 0} comments
            · ${formatRelativeTime(t.createdAt)}
          </div>
        </div>
      </div>
    `).join('');
    return createResponse(200, threadsHtml || '<p>No discussions yet. Be the first to start one!</p>', true);
  }

  return createResponse(200, { threads });
}

// Get a single thread with replies
async function getThread(threadId, userId, isHtmx = false) {
  // Find the thread (need to scan since we only have threadId)
  const scanResult = await docClient.send(new ScanCommand({
    TableName: process.env.THREADS_TABLE,
    FilterExpression: 'threadId = :id',
    ExpressionAttributeValues: { ':id': threadId },
  }));

  if (!scanResult.Items || scanResult.Items.length === 0) {
    if (isHtmx) {
      return createResponse(404, html.error('Thread not found'), true);
    }
    return createResponse(404, { error: 'Thread not found' });
  }

  const thread = scanResult.Items[0];

  // Get author info
  if (thread.authorId) {
    const author = await docClient.send(new GetCommand({
      TableName: process.env.USERS_TABLE,
      Key: { userId: thread.authorId },
    }));
    thread.authorNickname = author.Item?.nickname || 'Anonymous';
  }

  // Get replies
  const repliesResult = await docClient.send(new QueryCommand({
    TableName: process.env.REPLIES_TABLE,
    KeyConditionExpression: 'threadId = :id',
    ExpressionAttributeValues: { ':id': threadId },
  }));

  const replies = repliesResult.Items || [];

  // Enrich replies with author nicknames
  for (const reply of replies) {
    if (reply.authorId) {
      const author = await docClient.send(new GetCommand({
        TableName: process.env.USERS_TABLE,
        Key: { userId: reply.authorId },
      }));
      reply.authorNickname = author.Item?.nickname || 'Anonymous';
    }
  }

  // Sort replies by creation date
  replies.sort((a, b) => a.createdAt.localeCompare(b.createdAt));

  // Check user upvote status
  const hasUpvoted = userId ? false : false; // TODO: implement thread upvotes

  if (isHtmx) {
    const htmlContent = renderTemplate('thread_page', {
      ...thread,
      replies,
      isAuthenticated: !!userId,
      hasUpvoted,
    });
    return createResponse(200, htmlContent, true);
  }

  return createResponse(200, { thread, replies });
}

// Create a new thread
async function createThread(topicSlug, userId, data, isHtmx = false) {
  if (!userId) {
    if (isHtmx) {
      return createResponse(403, html.error('Please sign in to post'), true);
    }
    return createResponse(403, { error: 'Authentication required' });
  }

  if (!data.title || data.title.trim().length === 0) {
    if (isHtmx) {
      return createResponse(400, html.error('Title is required'), true);
    }
    return createResponse(400, { error: 'Title is required' });
  }

  const threadId = uuidv4();
  const timestamp = new Date().toISOString();

  const thread = {
    topicSlug,
    threadId,
    title: data.title.trim(),
    body: data.body?.trim() || '',
    authorId: userId,
    upvoteCount: 0,
    replyCount: 0,
    score: 0, // For GSI sorting
    createdAt: timestamp,
    updatedAt: timestamp,
  };

  await docClient.send(new PutCommand({
    TableName: process.env.THREADS_TABLE,
    Item: thread,
  }));

  if (isHtmx) {
    return createResponse(201, `
      <div class="status-message success">
        Thread created! <a href="/threads/${threadId}">View your thread</a>
      </div>
    `, true);
  }

  return createResponse(201, { threadId, ...thread });
}

// Create a reply to a thread
async function createReply(threadId, userId, data, isHtmx = false) {
  if (!userId) {
    if (isHtmx) {
      return createResponse(403, html.error('Please sign in to reply'), true);
    }
    return createResponse(403, { error: 'Authentication required' });
  }

  if (!data.body || data.body.trim().length === 0) {
    if (isHtmx) {
      return createResponse(400, html.error('Reply content is required'), true);
    }
    return createResponse(400, { error: 'Reply content is required' });
  }

  // Verify thread exists
  const threadScan = await docClient.send(new ScanCommand({
    TableName: process.env.THREADS_TABLE,
    FilterExpression: 'threadId = :id',
    ExpressionAttributeValues: { ':id': threadId },
  }));

  if (!threadScan.Items || threadScan.Items.length === 0) {
    if (isHtmx) {
      return createResponse(404, html.error('Thread not found'), true);
    }
    return createResponse(404, { error: 'Thread not found' });
  }

  const thread = threadScan.Items[0];
  const replyId = uuidv4();
  const timestamp = new Date().toISOString();

  // Calculate depth for nested replies (max depth: 5)
  let depth = 0;
  if (data.parentId) {
    const parentReply = await docClient.send(new GetCommand({
      TableName: process.env.REPLIES_TABLE,
      Key: { threadId, replyId: data.parentId },
    }));
    depth = Math.min((parentReply.Item?.depth || 0) + 1, 5);
  }

  const reply = {
    threadId,
    replyId,
    parentId: data.parentId || 'root',
    body: data.body.trim(),
    authorId: userId,
    depth,
    upvoteCount: 0,
    createdAt: timestamp,
  };

  await docClient.send(new PutCommand({
    TableName: process.env.REPLIES_TABLE,
    Item: reply,
  }));

  // Increment reply count on thread
  await docClient.send(new UpdateCommand({
    TableName: process.env.THREADS_TABLE,
    Key: { topicSlug: thread.topicSlug, threadId },
    UpdateExpression: 'SET replyCount = if_not_exists(replyCount, :zero) + :inc',
    ExpressionAttributeValues: { ':inc': 1, ':zero': 0 },
  }));

  // Get author nickname for response
  const author = await docClient.send(new GetCommand({
    TableName: process.env.USERS_TABLE,
    Key: { userId },
  }));
  const authorNickname = author.Item?.nickname || 'Anonymous';

  if (isHtmx) {
    return createResponse(201, `
      <div class="reply" style="margin-left: ${depth * 20}px">
        <div class="reply-meta">
          <a href="/user/${authorNickname}">${escapeHtml(authorNickname)}</a>
          · just now
        </div>
        <div class="reply-body">${escapeHtml(reply.body)}</div>
      </div>
    `, true);
  }

  return createResponse(201, { replyId, ...reply, authorNickname });
}

// ============================================
// Phase 8: Moderation Functions
// ============================================

// Flag content (event, thread, or reply)
async function createFlag(targetType, targetId, userId, reason, isHtmx = false) {
  if (!userId) {
    if (isHtmx) {
      return createResponse(403, html.error('Please sign in to flag content'), true);
    }
    return createResponse(403, { error: 'Authentication required' });
  }

  const validTargetTypes = ['event', 'thread', 'reply'];
  if (!validTargetTypes.includes(targetType)) {
    return createResponse(400, { error: 'Invalid target type' });
  }

  const flagId = uuidv4();
  const timestamp = new Date().toISOString();
  const targetKey = `${targetType}#${targetId}`;

  const flag = {
    targetKey,
    flagId,
    targetType,
    targetId,
    flaggedBy: userId,
    reason: reason?.trim() || 'No reason provided',
    status: 'pending', // pending, resolved, dismissed
    createdAt: timestamp,
  };

  await docClient.send(new PutCommand({
    TableName: process.env.FLAGS_TABLE,
    Item: flag,
  }));

  if (isHtmx) {
    return createResponse(201, '<div class="status-message success">Thank you for reporting. A moderator will review this content.</div>', true);
  }

  return createResponse(201, { flagId, message: 'Flag submitted' });
}

// List pending flags (admin only)
async function listPendingFlags(userId, isHtmx = false) {
  const isAdmin = await isUserAdmin(userId);
  if (!isAdmin) {
    if (isHtmx) {
      return createResponse(403, html.error('Admin access required'), true);
    }
    return createResponse(403, { error: 'Admin access required' });
  }

  const result = await docClient.send(new QueryCommand({
    TableName: process.env.FLAGS_TABLE,
    IndexName: 'pendingFlagsIndex',
    KeyConditionExpression: '#status = :pending',
    ExpressionAttributeNames: { '#status': 'status' },
    ExpressionAttributeValues: { ':pending': 'pending' },
    ScanIndexForward: false, // Newest first
    Limit: 50,
  }));

  const flags = result.Items || [];

  if (isHtmx) {
    if (flags.length === 0) {
      return createResponse(200, '<div class="empty-state">No pending flags. 🎉</div>', true);
    }

    const flagsHtml = flags.map(f => `
      <div class="flag-item" id="flag-${f.flagId}">
        <div class="flag-meta">
          <strong>${escapeHtml(f.targetType)}</strong>: ${escapeHtml(f.targetId)}
          <br>Reported ${formatRelativeTime(f.createdAt)}
        </div>
        <div class="flag-reason">${escapeHtml(f.reason)}</div>
        <div class="flag-actions">
          <button class="btn btn-danger btn-small" 
                  hx-post="/api/admin/flags/${f.flagId}/resolve"
                  hx-vals='{"action": "remove"}'
                  hx-target="#flag-${f.flagId}"
                  hx-swap="outerHTML">
            Remove Content
          </button>
          <button class="btn btn-secondary btn-small"
                  hx-post="/api/admin/flags/${f.flagId}/resolve"
                  hx-vals='{"action": "dismiss"}'
                  hx-target="#flag-${f.flagId}"
                  hx-swap="outerHTML">
            Dismiss
          </button>
          <a href="/${f.targetType === 'event' ? 'events' : f.targetType === 'thread' ? 'threads' : 'threads'}/${f.targetId}" 
             target="_blank" class="btn btn-link btn-small">
            View →
          </a>
        </div>
      </div>
    `).join('');

    return createResponse(200, `
      <div class="flags-list">
        <h3>${flags.length} Pending Flag${flags.length === 1 ? '' : 's'}</h3>
        ${flagsHtml}
      </div>
    `, true);
  }

  return createResponse(200, { flags });
}

// Resolve a flag (admin only)
async function resolveFlag(flagId, userId, action, isHtmx = false) {
  const isAdmin = await isUserAdmin(userId);
  if (!isAdmin) {
    return createResponse(403, { error: 'Admin access required' });
  }

  if (!['remove', 'dismiss'].includes(action)) {
    return createResponse(400, { error: 'Invalid action. Must be "remove" or "dismiss"' });
  }

  // Find the flag
  const flagResult = await docClient.send(new ScanCommand({
    TableName: process.env.FLAGS_TABLE,
    FilterExpression: 'flagId = :flagId',
    ExpressionAttributeValues: { ':flagId': flagId },
  }));

  if (!flagResult.Items || flagResult.Items.length === 0) {
    return createResponse(404, { error: 'Flag not found' });
  }

  const flag = flagResult.Items[0];
  const timestamp = new Date().toISOString();

  // Update flag status
  await docClient.send(new UpdateCommand({
    TableName: process.env.FLAGS_TABLE,
    Key: { targetKey: flag.targetKey, flagId },
    UpdateExpression: 'SET #status = :status, resolvedAt = :resolvedAt, resolvedBy = :resolvedBy, resolution = :resolution',
    ExpressionAttributeNames: { '#status': 'status' },
    ExpressionAttributeValues: {
      ':status': 'resolved',
      ':resolvedAt': timestamp,
      ':resolvedBy': userId,
      ':resolution': action,
    },
  }));

  // If action is 'remove', delete or hide the flagged content
  if (action === 'remove') {
    if (flag.targetType === 'event') {
      // Mark event as removed/hidden
      await docClient.send(new UpdateCommand({
        TableName: process.env.EVENTS_TABLE,
        Key: { eventId: flag.targetId },
        UpdateExpression: 'SET #status = :removed, removedAt = :removedAt, removedBy = :removedBy',
        ExpressionAttributeNames: { '#status': 'status' },
        ExpressionAttributeValues: {
          ':removed': 'removed',
          ':removedAt': timestamp,
          ':removedBy': userId,
        },
      }));
    } else if (flag.targetType === 'thread') {
      // Mark thread as removed
      await docClient.send(new UpdateCommand({
        TableName: process.env.THREADS_TABLE,
        Key: { topicSlug: flag.topicSlug || 'unknown', threadId: flag.targetId },
        UpdateExpression: 'SET #status = :removed',
        ExpressionAttributeNames: { '#status': 'status' },
        ExpressionAttributeValues: { ':removed': 'removed' },
      }));
    }
    // For replies, we could also add similar logic
  }

  if (isHtmx) {
    return createResponse(200, `<div class="status-message success">Flag ${action === 'remove' ? 'resolved - content removed' : 'dismissed'}</div>`, true);
  }

  return createResponse(200, { message: `Flag ${action === 'remove' ? 'resolved' : 'dismissed'}` });
}

// Shadowban a user (admin only) - their content won't be shown to others
async function shadowbanUser(targetUserId, adminUserId, isHtmx = false) {
  const isAdmin = await isUserAdmin(adminUserId);
  if (!isAdmin) {
    return createResponse(403, { error: 'Admin access required' });
  }

  const timestamp = new Date().toISOString();

  await docClient.send(new UpdateCommand({
    TableName: process.env.USERS_TABLE,
    Key: { userId: targetUserId },
    UpdateExpression: 'SET shadowbanned = :true, shadowbannedAt = :at, shadowbannedBy = :by',
    ExpressionAttributeValues: {
      ':true': true,
      ':at': timestamp,
      ':by': adminUserId,
    },
  }));

  if (isHtmx) {
    return createResponse(200, '<div class="status-message success">User shadowbanned</div>', true);
  }

  return createResponse(200, { message: 'User shadowbanned' });
}

// ============================================
// Phase 9: Email Notification Functions
// ============================================

// Send email via SES
async function sendEmail(to, subject, htmlBody, textBody = null) {
  // Only attempt to send if SES is configured
  const sourceEmail = process.env.SES_SOURCE_EMAIL;
  if (!sourceEmail) {
    console.log('SES_SOURCE_EMAIL not configured, skipping email send');
    return { success: false, reason: 'SES not configured' };
  }

  // Dynamically import SES client to avoid errors if not needed
  const { SESClient, SendEmailCommand } = await import('@aws-sdk/client-ses');
  const sesClient = new SESClient({});

  try {
    const params = {
      Source: sourceEmail,
      Destination: {
        ToAddresses: Array.isArray(to) ? to : [to],
      },
      Message: {
        Subject: {
          Data: subject,
          Charset: 'UTF-8',
        },
        Body: {
          Html: {
            Data: htmlBody,
            Charset: 'UTF-8',
          },
        },
      },
    };

    // Add text body if provided
    if (textBody) {
      params.Message.Body.Text = {
        Data: textBody,
        Charset: 'UTF-8',
      };
    }

    await sesClient.send(new SendEmailCommand(params));
    return { success: true };
  } catch (error) {
    console.error('Failed to send email:', error);
    return { success: false, reason: error.message };
  }
}

// Get user's email preferences
async function getEmailPrefs(userId) {
  const user = await docClient.send(new GetCommand({
    TableName: process.env.USERS_TABLE,
    Key: { userId },
    ProjectionExpression: 'emailPrefs, email',
  }));

  return user.Item?.emailPrefs || {
    replyNotifications: true,
    digestFrequency: 'weekly', // 'daily', 'weekly', 'never'
    eventReminders: true,
  };
}

// Send reply notification email
async function sendReplyNotification(threadId, replyAuthorNickname, replyBody, threadAuthorId) {
  // Get thread author's email preferences
  const prefs = await getEmailPrefs(threadAuthorId);
  if (!prefs.replyNotifications) {
    return { success: false, reason: 'User has disabled reply notifications' };
  }

  // Get thread author's email from Cognito
  const userResult = await docClient.send(new GetCommand({
    TableName: process.env.USERS_TABLE,
    Key: { userId: threadAuthorId },
  }));

  const userEmail = userResult.Item?.email;
  if (!userEmail) {
    return { success: false, reason: 'User email not found' };
  }

  const subject = `New reply to your discussion on DC Tech Events`;
  const htmlBody = `
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <h2 style="color: #2c3e50;">New Reply to Your Discussion</h2>
      <p><strong>${escapeHtml(replyAuthorNickname)}</strong> replied to your thread:</p>
      <blockquote style="border-left: 3px solid #3498db; padding-left: 15px; margin: 15px 0; color: #666;">
        ${escapeHtml(replyBody.substring(0, 300))}${replyBody.length > 300 ? '...' : ''}
      </blockquote>
      <p><a href="https://next.dctech.events/threads/${threadId}" style="color: #3498db;">View the full discussion →</a></p>
      <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
      <p style="color: #999; font-size: 12px;">
        You're receiving this because you have reply notifications enabled.
        <a href="https://next.dctech.events/settings" style="color: #999;">Manage your notification preferences</a>
      </p>
    </div>
  `;

  return await sendEmail(userEmail, subject, htmlBody);
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

  // ============================================
  // Phase 7: Discussion Board Routes
  // ============================================

  // GET /api/topics/{slug}/threads - List threads for a topic
  if (path.match(/^\/api\/topics\/[a-z0-9-]+\/threads\/?$/) && method === 'GET') {
    const slug = path.match(/^\/api\/topics\/([a-z0-9-]+)\/threads/)[1];
    const sort = queryParams.sort || 'new';
    return await listThreads(slug, sort, isHtmx);
  }

  // POST /api/topics/{slug}/threads - Create a new thread
  if (path.match(/^\/api\/topics\/[a-z0-9-]+\/threads\/?$/) && method === 'POST') {
    const slug = path.match(/^\/api\/topics\/([a-z0-9-]+)\/threads/)[1];
    return await createThread(slug, userId, body, isHtmx);
  }

  // GET /threads/{id} - View a thread
  if (path.match(/^\/threads\/[a-f0-9-]+\/?$/) && method === 'GET') {
    const threadId = path.match(/^\/threads\/([a-f0-9-]+)/)[1];
    return await getThread(threadId, userId, true); // Always return HTML
  }

  // POST /api/threads/{id}/replies - Create a reply
  if (path.match(/^\/api\/threads\/[a-f0-9-]+\/replies\/?$/) && method === 'POST') {
    const threadId = path.match(/^\/api\/threads\/([a-f0-9-]+)\/replies/)[1];
    return await createReply(threadId, userId, body, isHtmx);
  }

  // ============================================
  // Phase 8: Moderation Routes
  // ============================================

  // POST /api/flags - Create a new flag
  if (path === '/api/flags' && method === 'POST') {
    const { targetType, targetId, reason } = parsedBody || {};
    return await createFlag(targetType, targetId, userId, reason, isHtmx);
  }

  // GET /admin/moderation - Moderation queue page (admin only)
  if ((path === '/admin/moderation' || path === '/admin/moderation/') && method === 'GET') {
    if (!userId) {
      return {
        statusCode: 302,
        headers: { 'Location': cognitoLoginUrl('/admin/moderation') },
        body: '',
      };
    }
    const isAdmin = await isUserAdmin(userId);
    if (!isAdmin) {
      return createResponse(403, '<h1>Access Denied</h1><p>Admin access required.</p>', true);
    }

    // Get pending flags for the moderation queue
    const flagsResult = await listPendingFlags(userId, true);

    // Wrap in admin template
    const htmlContent = `
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Moderation Queue - DC Tech Events</title>
    <meta name="robots" content="noindex, nofollow">
    <link rel="stylesheet" href="/static/css/main.css">
    <script src="https://unpkg.com/htmx.org@2.0.4"></script>
    <style>
        .moderation-container { max-width: 900px; margin: 0 auto; }
        .flag-item { 
            background: white; 
            border: 1px solid var(--color-border); 
            border-radius: 8px; 
            padding: var(--spacing-lg); 
            margin-bottom: var(--spacing-md);
        }
        .flag-meta { color: var(--color-text-light); font-size: 0.875rem; margin-bottom: var(--spacing-sm); }
        .flag-reason { margin-bottom: var(--spacing-md); }
        .flag-actions { display: flex; gap: var(--spacing-sm); flex-wrap: wrap; }
        .btn-small { padding: 6px 12px; font-size: 0.875rem; }
    </style>
</head>
<body>
    {{> header}}
    <main class="container">
        <div class="moderation-container">
            <h1>Moderation Queue</h1>
            <p>Review and resolve reported content.</p>
            <div id="flags-container" hx-get="/api/admin/flags" hx-trigger="load" hx-swap="innerHTML">
                Loading...
            </div>
        </div>
    </main>
    {{> footer}}
</body>
</html>`;

    return createResponse(200, renderTemplate('layouts/base', { content: flagsResult.body, pageTitle: 'Moderation Queue' }), true);
  }

  // GET /api/admin/flags - List pending flags (admin only)
  if (path === '/api/admin/flags' && method === 'GET') {
    return await listPendingFlags(userId, isHtmx);
  }

  // POST /api/admin/flags/{id}/resolve - Resolve a flag (admin only)
  if (path.match(/^\/api\/admin\/flags\/[a-f0-9-]+\/resolve\/?$/) && method === 'POST') {
    const flagId = path.match(/^\/api\/admin\/flags\/([a-f0-9-]+)\/resolve/)[1];
    const { action } = parsedBody || {};
    return await resolveFlag(flagId, userId, action, isHtmx);
  }

  // POST /api/admin/users/{id}/shadowban - Shadowban a user (admin only)
  if (path.match(/^\/api\/admin\/users\/[^\/]+\/shadowban\/?$/) && method === 'POST') {
    const targetUserId = path.match(/^\/api\/admin\/users\/([^\/]+)\/shadowban/)[1];
    return await shadowbanUser(targetUserId, userId, isHtmx);
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

  // GET /robots.txt - Block search engine indexing (this is a staging site)
  if (path === '/robots.txt' && method === 'GET') {
    const robotsTxt = `# next.dctech.events - Development/Staging Site
# Do not index this site - production is at dctech.events
User-agent: *
Disallow: /
`;
    return {
      statusCode: 200,
      headers: {
        'Content-Type': 'text/plain',
      },
      body: robotsTxt,
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
  const requestHeaders = event.headers || {};
  
  try {
    const site = determineSite(event);
    const { path, method, body, pathParams, queryParams, userId, userEmail, isHtmx: isHtmxRequest } = await parseEvent(event);
    isHtmx = isHtmxRequest;

    // Handle CORS preflight
    if (method === 'OPTIONS') {
      return createResponse(200, {}, false, requestHeaders);
    }

    // Route to appropriate site handler
    // Note: Pass requestHeaders to handlers so they can forward to createResponse
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

    // Upvote routes
    if (path.match(/^\/api\/events\/[^/]+\/upvote$/) && method === 'POST') {
      if (!userId) {
        if (isHtmx) {
          return createResponse(200, '<a href="/login" class="upvote-btn">▲ Sign in to upvote</a>', true);
        }
        return createResponse(403, { error: 'Authentication required' });
      }
      const eventId = path.match(/^\/api\/events\/([^/]+)\/upvote$/)[1];
      return await upvoteEvent(eventId, userId, isHtmx);
    }
    if (path.match(/^\/api\/events\/[^/]+\/upvote$/) && method === 'DELETE') {
      if (!userId) {
        return createResponse(403, { error: 'Authentication required' });
      }
      const eventId = path.match(/^\/api\/events\/([^/]+)\/upvote$/)[1];
      return await removeUpvote(eventId, userId, isHtmx);
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

    return createResponse(404, { error: 'Not found' }, false, requestHeaders);
  } catch (error) {
    // Log error details for debugging but sanitize for users
    console.error('Error:', {
      name: error.name,
      message: error.message,
      stack: process.env.NODE_ENV === 'development' ? error.stack : undefined,
    });
    
    // Generic error message for production
    const userMessage = process.env.NODE_ENV === 'production'
      ? 'An unexpected error occurred. Please try again later.'
      : error.message;
    
    if (isHtmx) {
      return createResponse(500, html.error(userMessage), true, requestHeaders);
    }
    return createResponse(500, { error: userMessage }, false, requestHeaders);
  }
};
