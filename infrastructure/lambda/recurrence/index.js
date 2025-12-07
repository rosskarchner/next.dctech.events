/**
 * Recurrence Expansion Lambda
 * 
 * Runs daily to expand recurring events into individual instances for the next 90 days.
 * 
 * Recurrence Rules Supported:
 * - WEEKLY:DAY (e.g., "WEEKLY:TUE" = every Tuesday)
 * - MONTHLY:ORDINAL:DAY (e.g., "MONTHLY:3:WED" = 3rd Wednesday of each month)
 * - MONTHLY:LAST:DAY (e.g., "MONTHLY:LAST:THU" = last Thursday of each month)
 */

const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, ScanCommand, PutCommand, QueryCommand } = require('@aws-sdk/lib-dynamodb');
const { v4: uuidv4 } = require('uuid');

const dynamoClient = new DynamoDBClient({});
const docClient = DynamoDBDocumentClient.from(dynamoClient);

const EVENTS_TABLE = process.env.EVENTS_TABLE;
const EXPANSION_DAYS = 90; // How many days ahead to expand

// Day name to number mapping
const DAYS = {
    'SUN': 0, 'MON': 1, 'TUE': 2, 'WED': 3, 'THU': 4, 'FRI': 5, 'SAT': 6
};

// Format date as YYYY-MM-DD
function formatDate(date) {
    return date.toISOString().split('T')[0];
}

// Get the nth weekday of a month (e.g., 3rd Wednesday)
function getNthWeekdayOfMonth(year, month, dayOfWeek, n) {
    const firstDay = new Date(year, month, 1);
    let dayOffset = (dayOfWeek - firstDay.getDay() + 7) % 7;
    const firstOccurrence = new Date(year, month, 1 + dayOffset);

    const result = new Date(firstOccurrence);
    result.setDate(result.getDate() + (n - 1) * 7);

    // Check if still in the same month
    if (result.getMonth() !== month) {
        return null;
    }
    return result;
}

// Get the last weekday of a month
function getLastWeekdayOfMonth(year, month, dayOfWeek) {
    const lastDay = new Date(year, month + 1, 0); // Last day of month
    let dayOffset = (lastDay.getDay() - dayOfWeek + 7) % 7;
    return new Date(year, month, lastDay.getDate() - dayOffset);
}

// Parse recurrence rule and generate dates
function expandRecurrence(recurrenceRule, startDate, endDate) {
    const dates = [];
    const start = new Date(startDate);
    const end = new Date(endDate);

    const parts = recurrenceRule.split(':');
    const type = parts[0];

    if (type === 'WEEKLY') {
        const dayOfWeek = DAYS[parts[1]];
        if (dayOfWeek === undefined) return dates;

        // Find first occurrence on or after start
        let current = new Date(start);
        const daysUntil = (dayOfWeek - current.getDay() + 7) % 7;
        current.setDate(current.getDate() + daysUntil);

        // Generate weekly occurrences
        while (current <= end) {
            dates.push(formatDate(current));
            current.setDate(current.getDate() + 7);
        }
    } else if (type === 'MONTHLY') {
        if (parts[1] === 'LAST') {
            const dayOfWeek = DAYS[parts[2]];
            if (dayOfWeek === undefined) return dates;

            // Generate for each month in range
            let current = new Date(start.getFullYear(), start.getMonth(), 1);
            while (current <= end) {
                const occurrence = getLastWeekdayOfMonth(current.getFullYear(), current.getMonth(), dayOfWeek);
                if (occurrence >= start && occurrence <= end) {
                    dates.push(formatDate(occurrence));
                }
                current.setMonth(current.getMonth() + 1);
            }
        } else {
            const ordinal = parseInt(parts[1]);
            const dayOfWeek = DAYS[parts[2]];
            if (isNaN(ordinal) || dayOfWeek === undefined) return dates;

            // Generate for each month in range
            let current = new Date(start.getFullYear(), start.getMonth(), 1);
            while (current <= end) {
                const occurrence = getNthWeekdayOfMonth(current.getFullYear(), current.getMonth(), dayOfWeek, ordinal);
                if (occurrence && occurrence >= start && occurrence <= end) {
                    dates.push(formatDate(occurrence));
                }
                current.setMonth(current.getMonth() + 1);
            }
        }
    }

    return dates;
}

exports.handler = async (event) => {
    console.log('Starting recurrence expansion...');

    const today = new Date();
    const endDate = new Date();
    endDate.setDate(today.getDate() + EXPANSION_DAYS);

    const todayStr = formatDate(today);
    const endDateStr = formatDate(endDate);

    // Scan for events with recurrence rules
    const scanResult = await docClient.send(new ScanCommand({
        TableName: EVENTS_TABLE,
        FilterExpression: 'attribute_exists(recurrenceRule) AND recurrenceRule <> :empty',
        ExpressionAttributeValues: {
            ':empty': '',
        },
    }));

    const recurringEvents = scanResult.Items || [];
    console.log(`Found ${recurringEvents.length} recurring events`);

    let createdCount = 0;
    let skippedCount = 0;

    for (const parentEvent of recurringEvents) {
        const { eventId: parentEventId, recurrenceRule, title, time, location, description, groupId, topicSlug, createdBy, isNative, rsvpEnabled, showRsvpList } = parentEvent;

        // Generate dates for the expansion window
        const dates = expandRecurrence(recurrenceRule, todayStr, endDateStr);
        console.log(`Event "${title}" (${recurrenceRule}): ${dates.length} potential occurrences`);

        for (const eventDate of dates) {
            // Check if an instance already exists for this date
            const existingCheck = await docClient.send(new QueryCommand({
                TableName: EVENTS_TABLE,
                IndexName: 'dateEventsIndex',
                KeyConditionExpression: 'eventType = :eventType AND eventDate = :date',
                FilterExpression: 'parentEventId = :parentId',
                ExpressionAttributeValues: {
                    ':eventType': 'all',
                    ':date': eventDate,
                    ':parentId': parentEventId,
                },
            }));

            if (existingCheck.Items && existingCheck.Items.length > 0) {
                skippedCount++;
                continue; // Already exists
            }

            // Create new instance
            const instanceId = uuidv4();
            const timestamp = new Date().toISOString();

            await docClient.send(new PutCommand({
                TableName: EVENTS_TABLE,
                Item: {
                    eventId: instanceId,
                    parentEventId: parentEventId,
                    title,
                    eventDate,
                    time: time || '',
                    location: location || '',
                    description: description || '',
                    groupId: groupId || null,
                    topicSlug: topicSlug || null,
                    upvoteCount: 0,
                    eventType: 'all',
                    isNative: isNative || false,
                    rsvpEnabled: rsvpEnabled || false,
                    showRsvpList: showRsvpList !== false,
                    createdBy: createdBy || null,
                    createdAt: timestamp,
                    updatedAt: timestamp,
                    isRecurrenceInstance: true,
                },
            }));

            createdCount++;
        }
    }

    console.log(`Recurrence expansion complete: ${createdCount} created, ${skippedCount} skipped (already exist)`);

    return {
        statusCode: 200,
        body: JSON.stringify({
            message: 'Recurrence expansion complete',
            created: createdCount,
            skipped: skippedCount,
        }),
    };
};
