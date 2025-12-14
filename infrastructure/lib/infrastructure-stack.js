"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.InfrastructureStack = void 0;
const cdk = __importStar(require("aws-cdk-lib"));
const s3 = __importStar(require("aws-cdk-lib/aws-s3"));
const cloudfront = __importStar(require("aws-cdk-lib/aws-cloudfront"));
const origins = __importStar(require("aws-cdk-lib/aws-cloudfront-origins"));
const cognito = __importStar(require("aws-cdk-lib/aws-cognito"));
const dynamodb = __importStar(require("aws-cdk-lib/aws-dynamodb"));
const lambda = __importStar(require("aws-cdk-lib/aws-lambda"));
const apigateway = __importStar(require("aws-cdk-lib/aws-apigateway"));
const iam = __importStar(require("aws-cdk-lib/aws-iam"));
const eventbridge = __importStar(require("aws-cdk-lib/aws-events"));
const targets = __importStar(require("aws-cdk-lib/aws-events-targets"));
const certificatemanager = __importStar(require("aws-cdk-lib/aws-certificatemanager"));
const route53 = __importStar(require("aws-cdk-lib/aws-route53"));
const route53targets = __importStar(require("aws-cdk-lib/aws-route53-targets"));
const cloudwatch = __importStar(require("aws-cdk-lib/aws-cloudwatch"));
class InfrastructureStack extends cdk.Stack {
    constructor(scope, id, props) {
        super(scope, id, props);
        // ============================================
        // Cognito User Pool for Authentication
        // ============================================
        const userPool = new cognito.UserPool(this, 'OrganizeUserPool', {
            userPoolName: 'organize-dctech-events-users',
            selfSignUpEnabled: true,
            signInAliases: {
                email: true,
                username: true,
            },
            autoVerify: {
                email: true,
            },
            standardAttributes: {
                email: {
                    required: true,
                    mutable: true,
                },
                fullname: {
                    required: true,
                    mutable: true,
                },
            },
            customAttributes: {
                bio: new cognito.StringAttribute({ minLen: 0, maxLen: 500, mutable: true }),
                website: new cognito.StringAttribute({ minLen: 0, maxLen: 200, mutable: true }),
            },
            passwordPolicy: {
                minLength: 8,
                requireLowercase: true,
                requireUppercase: true,
                requireDigits: true,
                requireSymbols: false,
            },
            accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
        });
        // Social Identity Providers (Google, GitHub, etc.)
        // These are managed MANUALLY via AWS Console or CLI - not by CDK!
        // This allows you to add/update OAuth credentials without redeploying the stack.
        //
        // To add a provider:
        // 1. Go to AWS Cognito Console → User Pools → organize-dctech-events-users
        // 2. Click "Sign-in experience" → "Add identity provider"
        // 3. Or use AWS CLI (see SOCIAL_LOGIN.md for examples)
        //
        // The UserPoolClient below supports all identity providers - you just need to
        // tell it which ones are available by updating the list here when you add them.
        const identityProviders = [
            cognito.UserPoolClientIdentityProvider.COGNITO, // Username/password
            // Add more as you create them manually:
            // cognito.UserPoolClientIdentityProvider.GOOGLE,
            // cognito.UserPoolClientIdentityProvider.custom('GitHub'),
        ];
        const userPoolClient = new cognito.UserPoolClient(this, 'OrganizeUserPoolClient', {
            userPool,
            authFlows: {
                userPassword: true,
                userSrp: true,
            },
            generateSecret: false,
            // supportedIdentityProviders is intentionally commented out
            // This allows ALL configured providers (manual + CDK-managed) to work
            // supportedIdentityProviders: identityProviders,
            oAuth: {
                flows: {
                    authorizationCodeGrant: true,
                    implicitCodeGrant: true,
                },
                scopes: [
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.PROFILE,
                ],
                callbackUrls: [
                    'https://next.dctech.events/callback',
                    'http://localhost:3000/callback',
                    ...(props?.domainName ? [`https://${props.domainName}/callback`] : [])
                ],
                logoutUrls: [
                    'https://next.dctech.events',
                    'http://localhost:3000',
                    ...(props?.domainName ? [`https://${props.domainName}`] : [])
                ],
            },
        });
        // Use provided domain prefix or generate one with account ID for uniqueness
        // Cognito domain prefixes must be globally unique across all AWS regions
        const cognitoDomainPrefix = props?.cognitoDomainPrefix ||
            `organize-dctech-${cdk.Stack.of(this).account}`;
        const userPoolDomain = userPool.addDomain('OrganizeUserPoolDomain', {
            cognitoDomain: {
                domainPrefix: cognitoDomainPrefix,
            },
        });
        // Create 'admin' group for privileged users (topic creation, moderation, etc.)
        new cognito.CfnUserPoolGroup(this, 'AdminGroup', {
            userPoolId: userPool.userPoolId,
            groupName: 'admin',
            description: 'Administrators who can create topics and moderate content',
        });
        // ============================================
        // DynamoDB Tables
        // ============================================
        // Users table (for extended profile info beyond Cognito)
        const usersTable = new dynamodb.Table(this, 'UsersTable', {
            tableName: 'organize-users',
            partitionKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        });
        // Add GSI for nickname lookups (public profile pages)
        usersTable.addGlobalSecondaryIndex({
            indexName: 'nicknameIndex',
            partitionKey: { name: 'nickname', type: dynamodb.AttributeType.STRING },
        });
        // Groups table
        const groupsTable = new dynamodb.Table(this, 'GroupsTable', {
            tableName: 'organize-groups',
            partitionKey: { name: 'groupId', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        });
        // Add GSI for active groups
        groupsTable.addGlobalSecondaryIndex({
            indexName: 'activeGroupsIndex',
            partitionKey: { name: 'active', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'createdAt', type: dynamodb.AttributeType.STRING },
        });
        // Group Members table
        const groupMembersTable = new dynamodb.Table(this, 'GroupMembersTable', {
            tableName: 'organize-group-members',
            partitionKey: { name: 'groupId', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        });
        // Add GSI for user's groups
        groupMembersTable.addGlobalSecondaryIndex({
            indexName: 'userGroupsIndex',
            partitionKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'groupId', type: dynamodb.AttributeType.STRING },
        });
        // Events table
        const eventsTable = new dynamodb.Table(this, 'EventsTable', {
            tableName: 'organize-events',
            partitionKey: { name: 'eventId', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        });
        // Add GSI for events by group
        eventsTable.addGlobalSecondaryIndex({
            indexName: 'groupEventsIndex',
            partitionKey: { name: 'groupId', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'eventDate', type: dynamodb.AttributeType.STRING },
        });
        // Add GSI for events by date
        eventsTable.addGlobalSecondaryIndex({
            indexName: 'dateEventsIndex',
            partitionKey: { name: 'eventType', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'eventDate', type: dynamodb.AttributeType.STRING },
        });
        // RSVPs table
        const rsvpsTable = new dynamodb.Table(this, 'RSVPsTable', {
            tableName: 'organize-rsvps',
            partitionKey: { name: 'eventId', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        });
        // Add GSI for user's RSVPs
        rsvpsTable.addGlobalSecondaryIndex({
            indexName: 'userRSVPsIndex',
            partitionKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'eventId', type: dynamodb.AttributeType.STRING },
        });
        // Messages table
        const messagesTable = new dynamodb.Table(this, 'MessagesTable', {
            tableName: 'organize-messages',
            partitionKey: { name: 'groupId', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        });
        // Topics table (for community hub categories)
        const topicsTable = new dynamodb.Table(this, 'TopicsTable', {
            tableName: 'organize-topics',
            partitionKey: { name: 'slug', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        });
        // Add GSI for groups by topic
        groupsTable.addGlobalSecondaryIndex({
            indexName: 'topicIndex',
            partitionKey: { name: 'topicSlug', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'name', type: dynamodb.AttributeType.STRING },
        });
        // Add GSI for events by topic
        eventsTable.addGlobalSecondaryIndex({
            indexName: 'topicIndex',
            partitionKey: { name: 'topicSlug', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'eventDate', type: dynamodb.AttributeType.STRING },
        });
        // TopicFollows table (user topic subscriptions)
        const topicFollowsTable = new dynamodb.Table(this, 'TopicFollowsTable', {
            tableName: 'organize-topic-follows',
            partitionKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'topicSlug', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        });
        // Add GSI for getting followers of a topic
        topicFollowsTable.addGlobalSecondaryIndex({
            indexName: 'topicFollowersIndex',
            partitionKey: { name: 'topicSlug', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
        });
        // EventUpvotes table (for tracking event upvotes)
        const eventUpvotesTable = new dynamodb.Table(this, 'EventUpvotesTable', {
            tableName: 'organize-event-upvotes',
            partitionKey: { name: 'eventId', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        });
        // Add GSI for getting all upvotes by a user
        eventUpvotesTable.addGlobalSecondaryIndex({
            indexName: 'userUpvotesIndex',
            partitionKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'eventId', type: dynamodb.AttributeType.STRING },
        });
        // ============================================
        // Phase 7: Discussion Boards Tables
        // ============================================
        // Threads table (for topic discussions)
        const threadsTable = new dynamodb.Table(this, 'ThreadsTable', {
            tableName: 'organize-threads',
            partitionKey: { name: 'topicSlug', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'threadId', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        });
        // Add GSI for getting threads by creation date (for "New" sorting)
        threadsTable.addGlobalSecondaryIndex({
            indexName: 'threadsByDateIndex',
            partitionKey: { name: 'topicSlug', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'createdAt', type: dynamodb.AttributeType.STRING },
        });
        // Add GSI for getting threads by score (for "Hot" sorting)
        threadsTable.addGlobalSecondaryIndex({
            indexName: 'threadsByScoreIndex',
            partitionKey: { name: 'topicSlug', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'score', type: dynamodb.AttributeType.NUMBER },
        });
        // Replies table (for thread comments)
        const repliesTable = new dynamodb.Table(this, 'RepliesTable', {
            tableName: 'organize-replies',
            partitionKey: { name: 'threadId', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'replyId', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        });
        // Add GSI for getting replies by parent (for nested threading)
        repliesTable.addGlobalSecondaryIndex({
            indexName: 'repliesByParentIndex',
            partitionKey: { name: 'parentId', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'createdAt', type: dynamodb.AttributeType.STRING },
        });
        // ============================================
        // Phase 8: Moderation Tables
        // ============================================
        // Flags table (for content moderation)
        const flagsTable = new dynamodb.Table(this, 'FlagsTable', {
            tableName: 'organize-flags',
            partitionKey: { name: 'targetKey', type: dynamodb.AttributeType.STRING }, // Format: targetType#targetId
            sortKey: { name: 'flagId', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
        });
        // Add GSI for getting pending flags (for moderation queue)
        flagsTable.addGlobalSecondaryIndex({
            indexName: 'pendingFlagsIndex',
            partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'createdAt', type: dynamodb.AttributeType.STRING },
        });
        // ============================================
        // S3 Bucket for Static Website and Exports
        // ============================================
        const websiteBucket = new s3.Bucket(this, 'OrganizeWebsiteBucket', {
            websiteIndexDocument: 'index.html',
            websiteErrorDocument: 'index.html',
            publicReadAccess: false,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            removalPolicy: cdk.RemovalPolicy.RETAIN,
            cors: [
                {
                    allowedOrigins: ['*'],
                    allowedMethods: [s3.HttpMethods.GET],
                    allowedHeaders: ['*'],
                },
            ],
        });
        // Origin Access Control for CloudFront to access S3
        const originAccessControl = new cloudfront.S3OriginAccessControl(this, 'NextOAC', {
            description: 'OAC for next.dctech.events static assets',
        });
        // ============================================
        // Route 53 and SSL Certificate
        // ============================================
        // Look up the existing hosted zone for dctech.events
        const hostedZone = route53.HostedZone.fromLookup(this, 'DcTechHostedZone', {
            domainName: 'dctech.events',
        });
        // Create ACM certificate for next.dctech.events (must be in us-east-1 for CloudFront)
        let certificate;
        if (cdk.Stack.of(this).region !== 'us-east-1') {
            throw new Error('ACM certificates for CloudFront must be created in us-east-1. ' +
                'Deploy this stack to us-east-1.');
        }
        certificate = new certificatemanager.Certificate(this, 'NextDcTechCertificate', {
            domainName: 'next.dctech.events',
            validation: certificatemanager.CertificateValidation.fromDns(hostedZone),
        });
        // ============================================
        // CloudFront Distribution for next.dctech.events
        // ============================================
        // ============================================
        // Lambda Functions for API
        // ============================================
        // Environment variables for Lambda functions
        const lambdaEnv = {
            USERS_TABLE: usersTable.tableName,
            GROUPS_TABLE: groupsTable.tableName,
            GROUP_MEMBERS_TABLE: groupMembersTable.tableName,
            EVENTS_TABLE: eventsTable.tableName,
            RSVPS_TABLE: rsvpsTable.tableName,
            MESSAGES_TABLE: messagesTable.tableName,
            TOPICS_TABLE: topicsTable.tableName,
            TOPIC_FOLLOWS_TABLE: topicFollowsTable.tableName,
            EVENT_UPVOTES_TABLE: eventUpvotesTable.tableName,
            THREADS_TABLE: threadsTable.tableName,
            REPLIES_TABLE: repliesTable.tableName,
            FLAGS_TABLE: flagsTable.tableName,
            USER_POOL_ID: userPool.userPoolId,
            USER_POOL_CLIENT_ID: userPoolClient.userPoolClientId,
            USER_POOL_REGION: cdk.Stack.of(this).region,
            COGNITO_DOMAIN: userPoolDomain.domainName,
            WEBSITE_BUCKET: websiteBucket.bucketName,
            NEXT_DCTECH_DOMAIN: this.node.tryGetContext('nextDomain') || 'next.dctech.events',
            GITHUB_REPO: this.node.tryGetContext('githubRepo') || 'rosskarchner/dctech.events',
            // Phase 9: Email Notifications
            SES_SOURCE_EMAIL: 'outgoing@dctech.events',
        };
        // Lambda Layer for Handlebars templates
        const templatesLayer = new lambda.LayerVersion(this, 'TemplatesLayer', {
            code: lambda.Code.fromAsset('lambda/layers/templates'),
            compatibleRuntimes: [lambda.Runtime.NODEJS_20_X],
            description: 'Handlebars templates for both organize and next.dctech.events',
        });
        // API Lambda function (handles all API routes)
        const apiFunction = new lambda.Function(this, 'ApiFunction', {
            runtime: lambda.Runtime.NODEJS_20_X,
            handler: 'index.handler',
            code: lambda.Code.fromAsset('lambda/api'),
            environment: lambdaEnv,
            timeout: cdk.Duration.seconds(30),
            memorySize: 512,
            tracing: lambda.Tracing.ACTIVE, // Enable X-Ray tracing
            layers: [templatesLayer],
        });
        // Grant permissions to Lambda
        usersTable.grantReadWriteData(apiFunction);
        groupsTable.grantReadWriteData(apiFunction);
        groupMembersTable.grantReadWriteData(apiFunction);
        eventsTable.grantReadWriteData(apiFunction);
        rsvpsTable.grantReadWriteData(apiFunction);
        messagesTable.grantReadWriteData(apiFunction);
        topicsTable.grantReadWriteData(apiFunction);
        topicFollowsTable.grantReadWriteData(apiFunction);
        eventUpvotesTable.grantReadWriteData(apiFunction);
        threadsTable.grantReadWriteData(apiFunction);
        repliesTable.grantReadWriteData(apiFunction);
        flagsTable.grantReadWriteData(apiFunction);
        // Phase 9: Grant SES permissions for email notifications
        apiFunction.addToRolePolicy(new iam.PolicyStatement({
            actions: ['ses:SendEmail', 'ses:SendRawEmail'],
            resources: [
                `arn:aws:ses:${this.region}:${this.account}:identity/dctech.events`,
                `arn:aws:ses:${this.region}:${this.account}:identity/outgoing@dctech.events`,
            ],
        }));
        // Grant Cognito permissions
        apiFunction.addToRolePolicy(new iam.PolicyStatement({
            actions: ['cognito-idp:GetUser', 'cognito-idp:AdminGetUser', 'cognito-idp:AdminListGroupsForUser'],
            resources: [userPool.userPoolArn],
        }));
        // Export Lambda function (generates groups.yaml and events.yaml)
        const exportFunction = new lambda.Function(this, 'ExportFunction', {
            runtime: lambda.Runtime.NODEJS_20_X,
            handler: 'index.handler',
            code: lambda.Code.fromAsset('lambda/export'),
            environment: lambdaEnv,
            timeout: cdk.Duration.minutes(5),
            memorySize: 1024,
            tracing: lambda.Tracing.ACTIVE, // Enable X-Ray tracing
        });
        // Grant permissions to export Lambda
        groupsTable.grantReadData(exportFunction);
        eventsTable.grantReadData(exportFunction);
        websiteBucket.grantWrite(exportFunction);
        // Schedule export to run every 5 minutes
        const exportRule = new eventbridge.Rule(this, 'ExportSchedule', {
            schedule: eventbridge.Schedule.rate(cdk.Duration.minutes(5)),
        });
        exportRule.addTarget(new targets.LambdaFunction(exportFunction));
        // Refresh Lambda function (syncs groups/events from GitHub and fetches iCal feeds)
        const refreshFunction = new lambda.DockerImageFunction(this, 'RefreshFunction', {
            code: lambda.DockerImageCode.fromImageAsset('lambda/refresh'),
            environment: {
                ...lambdaEnv,
                GITHUB_TOKEN_SECRET: 'dctech-events/github-token',
            },
            timeout: cdk.Duration.minutes(5),
            memorySize: 1024,
            description: 'Fetches iCal feeds and syncs from GitHub repo',
            tracing: lambda.Tracing.ACTIVE,
        });
        // Grant permissions to refresh Lambda
        groupsTable.grantReadWriteData(refreshFunction);
        eventsTable.grantReadWriteData(refreshFunction);
        // Grant permission to read GitHub token from Secrets Manager
        refreshFunction.addToRolePolicy(new iam.PolicyStatement({
            actions: ['secretsmanager:GetSecretValue'],
            resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:dctech-events/github-token-*`],
        }));
        // Schedule refresh to run every 4 hours
        const refreshRule = new eventbridge.Rule(this, 'RefreshSchedule', {
            schedule: eventbridge.Schedule.rate(cdk.Duration.hours(4)),
            description: 'Refresh iCal feeds from 110+ groups and sync from GitHub',
        });
        refreshRule.addTarget(new targets.LambdaFunction(refreshFunction));
        // ============================================
        // Phase 6: Recurrence Expansion Lambda
        // ============================================
        // Recurrence Lambda (expands recurring events into instances)
        const recurrenceFunction = new lambda.Function(this, 'RecurrenceFunction', {
            runtime: lambda.Runtime.NODEJS_20_X,
            handler: 'index.handler',
            code: lambda.Code.fromAsset('lambda/recurrence'),
            environment: {
                EVENTS_TABLE: eventsTable.tableName,
            },
            timeout: cdk.Duration.minutes(2),
            memorySize: 256,
            description: 'Expands recurring events into individual instances',
        });
        // Grant permissions to recurrence Lambda
        eventsTable.grantReadWriteData(recurrenceFunction);
        // Schedule recurrence expansion to run daily at 2am UTC
        const recurrenceRule = new eventbridge.Rule(this, 'RecurrenceSchedule', {
            schedule: eventbridge.Schedule.cron({ hour: '2', minute: '0' }),
            description: 'Daily expansion of recurring events',
        });
        recurrenceRule.addTarget(new targets.LambdaFunction(recurrenceFunction));
        // ============================================
        // CloudWatch Alarms for Monitoring
        // ============================================
        // API Lambda error alarm
        const apiErrorAlarm = new cloudwatch.Alarm(this, 'ApiLambdaErrorAlarm', {
            metric: apiFunction.metricErrors({
                statistic: 'Sum',
                period: cdk.Duration.minutes(5),
            }),
            threshold: 10,
            evaluationPeriods: 1,
            alarmDescription: 'Alert when API Lambda function has more than 10 errors in 5 minutes',
            alarmName: 'organize-api-lambda-errors',
            treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        });
        // Export Lambda error alarm
        const exportErrorAlarm = new cloudwatch.Alarm(this, 'ExportLambdaErrorAlarm', {
            metric: exportFunction.metricErrors({
                statistic: 'Sum',
                period: cdk.Duration.minutes(5),
            }),
            threshold: 3,
            evaluationPeriods: 2,
            alarmDescription: 'Alert when Export Lambda function has more than 3 errors in 10 minutes',
            alarmName: 'organize-export-lambda-errors',
            treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        });
        // DynamoDB throttle alarms for critical tables
        const groupsThrottleAlarm = new cloudwatch.Alarm(this, 'GroupsTableThrottleAlarm', {
            metric: groupsTable.metricUserErrors({
                statistic: 'Sum',
                period: cdk.Duration.minutes(5),
            }),
            threshold: 5,
            evaluationPeriods: 2,
            alarmDescription: 'Alert when Groups table experiences throttling',
            alarmName: 'organize-groups-table-throttle',
            treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        });
        const eventsThrottleAlarm = new cloudwatch.Alarm(this, 'EventsTableThrottleAlarm', {
            metric: eventsTable.metricUserErrors({
                statistic: 'Sum',
                period: cdk.Duration.minutes(5),
            }),
            threshold: 5,
            evaluationPeriods: 2,
            alarmDescription: 'Alert when Events table experiences throttling',
            alarmName: 'organize-events-table-throttle',
            treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        });
        // ============================================
        // API Gateway
        // ============================================
        const api = new apigateway.RestApi(this, 'OrganizeApi', {
            restApiName: 'Organize DC Tech Events API',
            description: 'API for organize.dctech.events',
            defaultCorsPreflightOptions: {
                allowOrigins: [
                    'https://next.dctech.events',
                    'http://localhost:3000', // For local development
                ],
                allowMethods: apigateway.Cors.ALL_METHODS,
                allowHeaders: [
                    'Content-Type',
                    'X-Amz-Date',
                    'Authorization',
                    'X-Api-Key',
                    'X-Amz-Security-Token',
                    'HX-Request',
                    'HX-Target',
                    'HX-Trigger',
                ],
            },
        });
        // Lambda integration
        const apiIntegration = new apigateway.LambdaIntegration(apiFunction);
        // Use proxy resource to avoid Lambda permission policy size limits
        // This creates a single permission instead of one per route
        // The Lambda function handles all routing and authentication internally
        // Note: Authorizer is removed to support both public and protected routes
        // The Lambda validates Cognito tokens when present and checks permissions per route
        // Add root path handler
        api.root.addMethod('ANY', apiIntegration);
        // Add proxy resource for all other paths
        const proxy = api.root.addResource('{proxy+}');
        proxy.addMethod('ANY', apiIntegration);
        // ============================================
        // CloudFront Distribution for next.dctech.events
        // ============================================
        // Import existing Web ACL (created by CloudFront pricing plan subscription)
        const webAclArn = 'arn:aws:wafv2:us-east-1:797438674243:global/webacl/CreatedByCloudFront-08f900a2/598f35c6-3323-4a6b-9ae9-9cd73d9e7ca0';
        const distribution = new cloudfront.Distribution(this, 'NextDcTechDistribution', {
            webAclId: webAclArn,
            defaultBehavior: {
                origin: new origins.HttpOrigin(`${api.restApiId}.execute-api.${this.region}.amazonaws.com`, {
                    originPath: '/prod',
                    customHeaders: {
                        'X-Site': 'next',
                    },
                }),
                viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
                cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED, // Disable caching for dynamic content
                originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER, // Forward query strings for OAuth callback
            },
            additionalBehaviors: {
                '/static/*': {
                    origin: origins.S3BucketOrigin.withOriginAccessControl(websiteBucket, {
                        originAccessControl,
                    }),
                    viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
                },
            },
            domainNames: ['next.dctech.events'],
            certificate,
        });
        // Grant CloudFront OAC read access to S3 bucket for static assets
        websiteBucket.addToResourcePolicy(new iam.PolicyStatement({
            effect: iam.Effect.ALLOW,
            principals: [new iam.ServicePrincipal('cloudfront.amazonaws.com')],
            actions: ['s3:GetObject'],
            resources: [websiteBucket.arnForObjects('*')],
            conditions: {
                StringEquals: {
                    'AWS:SourceArn': `arn:aws:cloudfront::${this.account}:distribution/${distribution.distributionId}`,
                },
            },
        }));
        // Add Route 53 A & AAAA records for next.dctech.events
        new route53.ARecord(this, 'NextDcTechARecord', {
            zone: hostedZone,
            recordName: 'next',
            target: route53.RecordTarget.fromAlias(new route53targets.CloudFrontTarget(distribution)),
        });
        new route53.AaaaRecord(this, 'NextDcTechAaaaRecord', {
            zone: hostedZone,
            recordName: 'next',
            target: route53.RecordTarget.fromAlias(new route53targets.CloudFrontTarget(distribution)),
        });
        // ============================================
        // IAM User for GitHub Actions
        // ============================================
        // Create IAM user for GitHub Actions to deploy static assets
        const githubActionsUser = new iam.User(this, 'GitHubActionsUser', {
            userName: 'github-actions-frontend-deploy',
        });
        // Create policy for deploying to S3 and invalidating CloudFront
        const frontendDeployPolicy = new iam.Policy(this, 'FrontendDeployPolicy', {
            policyName: 'frontend-deploy-policy',
            statements: [
                // S3 permissions to upload static assets
                new iam.PolicyStatement({
                    effect: iam.Effect.ALLOW,
                    actions: [
                        's3:PutObject',
                        's3:PutObjectAcl',
                        's3:DeleteObject',
                        's3:ListBucket',
                    ],
                    resources: [
                        websiteBucket.bucketArn,
                        `${websiteBucket.bucketArn}/*`,
                    ],
                }),
                // CloudFront permissions to invalidate cache
                new iam.PolicyStatement({
                    effect: iam.Effect.ALLOW,
                    actions: ['cloudfront:CreateInvalidation'],
                    resources: [`arn:aws:cloudfront::${this.account}:distribution/${distribution.distributionId}`],
                }),
            ],
        });
        // Attach policy to user
        frontendDeployPolicy.attachToUser(githubActionsUser);
        // ============================================
        // Outputs
        // ============================================
        new cdk.CfnOutput(this, 'UserPoolId', {
            value: userPool.userPoolId,
            description: 'Cognito User Pool ID',
        });
        new cdk.CfnOutput(this, 'UserPoolClientId', {
            value: userPoolClient.userPoolClientId,
            description: 'Cognito User Pool Client ID',
        });
        new cdk.CfnOutput(this, 'UserPoolDomain', {
            value: userPoolDomain.domainName,
            description: 'Cognito User Pool Domain',
        });
        new cdk.CfnOutput(this, 'ApiUrl', {
            value: api.url,
            description: 'API Gateway URL',
        });
        new cdk.CfnOutput(this, 'CloudFrontUrl', {
            value: distribution.distributionDomainName,
            description: 'CloudFront Distribution URL for next.dctech.events',
        });
        new cdk.CfnOutput(this, 'WebsiteBucketName', {
            value: websiteBucket.bucketName,
            description: 'S3 Website Bucket Name',
        });
        new cdk.CfnOutput(this, 'CloudFrontDistributionId', {
            value: distribution.distributionId,
            description: 'CloudFront Distribution ID (for GitHub Actions)',
        });
        new cdk.CfnOutput(this, 'GitHubActionsUserName', {
            value: githubActionsUser.userName,
            description: 'IAM User for GitHub Actions (create access keys for this user)',
        });
    }
}
exports.InfrastructureStack = InfrastructureStack;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiaW5mcmFzdHJ1Y3R1cmUtc3RhY2suanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyJpbmZyYXN0cnVjdHVyZS1zdGFjay50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7QUFBQSxpREFBbUM7QUFFbkMsdURBQXlDO0FBQ3pDLHVFQUF5RDtBQUN6RCw0RUFBOEQ7QUFDOUQsaUVBQW1EO0FBQ25ELG1FQUFxRDtBQUNyRCwrREFBaUQ7QUFDakQsdUVBQXlEO0FBQ3pELHlEQUEyQztBQUMzQyxvRUFBc0Q7QUFDdEQsd0VBQTBEO0FBQzFELHVGQUF5RTtBQUN6RSxpRUFBbUQ7QUFDbkQsZ0ZBQWtFO0FBQ2xFLHVFQUF5RDtBQVd6RCxNQUFhLG1CQUFvQixTQUFRLEdBQUcsQ0FBQyxLQUFLO0lBQ2hELFlBQVksS0FBZ0IsRUFBRSxFQUFVLEVBQUUsS0FBZ0M7UUFDeEUsS0FBSyxDQUFDLEtBQUssRUFBRSxFQUFFLEVBQUUsS0FBSyxDQUFDLENBQUM7UUFFeEIsK0NBQStDO1FBQy9DLHVDQUF1QztRQUN2QywrQ0FBK0M7UUFDL0MsTUFBTSxRQUFRLEdBQUcsSUFBSSxPQUFPLENBQUMsUUFBUSxDQUFDLElBQUksRUFBRSxrQkFBa0IsRUFBRTtZQUM5RCxZQUFZLEVBQUUsOEJBQThCO1lBQzVDLGlCQUFpQixFQUFFLElBQUk7WUFDdkIsYUFBYSxFQUFFO2dCQUNiLEtBQUssRUFBRSxJQUFJO2dCQUNYLFFBQVEsRUFBRSxJQUFJO2FBQ2Y7WUFDRCxVQUFVLEVBQUU7Z0JBQ1YsS0FBSyxFQUFFLElBQUk7YUFDWjtZQUNELGtCQUFrQixFQUFFO2dCQUNsQixLQUFLLEVBQUU7b0JBQ0wsUUFBUSxFQUFFLElBQUk7b0JBQ2QsT0FBTyxFQUFFLElBQUk7aUJBQ2Q7Z0JBQ0QsUUFBUSxFQUFFO29CQUNSLFFBQVEsRUFBRSxJQUFJO29CQUNkLE9BQU8sRUFBRSxJQUFJO2lCQUNkO2FBQ0Y7WUFDRCxnQkFBZ0Is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QyxLQUFLLEVBQUUsY0FBYyxDQUFDLGdCQUFnQjtZQUN0QyxXQUFXLEVBQUUsNkJBQTZCO1NBQzNDLENBQUMsQ0FBQztRQUVILElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUsZ0JBQWdCLEVBQUU7WUFDeEMsS0FBSyxFQUFFLGNBQWMsQ0FBQyxVQUFVO1lBQ2hDLFdBQVcsRUFBRSwwQkFBMEI7U0FDeEMsQ0FBQyxDQUFDO1FBRUgsSUFBSSxHQUFHLENBQUMsU0FBUyxDQUFDLElBQUksRUFBRSxRQUFRLEVBQUU7WUFDaEMsS0FBSyxFQUFFLEdBQUcsQ0FBQyxHQUFHO1lBQ2QsV0FBVyxFQUFFLGlCQUFpQjtTQUMvQixDQUFDLENBQUM7UUFFSCxJQUFJLEdBQUcsQ0FBQyxTQUFTLENBQUMsSUFBSSxFQUFFLGVBQWUsRUFBRTtZQUN2QyxLQUFLLEVBQUUsWUFBWSxDQUFDLHNCQUFzQjtZQUMxQyxXQUFXLEVBQUUsb0RBQW9EO1NBQ2xFLENBQUMsQ0FBQztRQUVILElBQUksR0FBRyxDQUFDLFNBQVMsQ0FBQyxJQUFJLEVBQUUsbUJBQW1CLEVBQUU7WUFDM0MsS0FBSyxFQUFFLGFBQWEsQ0FBQyxVQUFVO1lBQy9CLFdBQVcsRUFBRSx3QkFBd0I7U0FDdEMsQ0FBQyxDQUFDO1FBRUgsSUFBSSxHQUFHLENBQUMsU0FBUyxDQUFDLElBQUksRUFBRSwwQkFBMEIsRUFBRTtZQUNsRCxLQUFLLEVBQUUsWUFBWSxDQUFDLGNBQWM7WUFDbEMsV0FBVyxFQUFFLGlEQUFpRDtTQUMvRCxDQUFDLENBQUM7UUFFSCxJQUFJLEdBQUcsQ0FBQyxTQUFTLENBQUMsSUFBSSxFQUFFLHVCQUF1QixFQUFFO1lBQy9DLEtBQUssRUFBRSxpQkFBaUIsQ0FBQyxRQUFRO1lBQ2pDLFdBQVcsRUFBRSxnRUFBZ0U7U0FDOUUsQ0FBQyxDQUFDO0lBQ0wsQ0FBQztDQUNGO0FBM3hCRCxrREEyeEJDIiwic291cmNlc0NvbnRlbnQiOlsiaW1wb3J0ICogYXMgY2RrIGZyb20gJ2F3cy1jZGstbGliJztcbmltcG9ydCB7IENvbnN0cnVjdCB9IGZyb20gJ2NvbnN0cnVjdHMnO1xuaW1wb3J0ICogYXMgczMgZnJvbSAnYXdzLWNkay1saWIvYXdzLXMzJztcbmltcG9ydCAqIGFzIGNsb3VkZnJvbnQgZnJvbSAnYXdzLWNkay1saWIvYXdzLWNsb3VkZnJvbnQnO1xuaW1wb3J0ICogYXMgb3JpZ2lucyBmcm9tICdhd3MtY2RrLWxpYi9hd3MtY2xvdWRmcm9udC1vcmlnaW5zJztcbmltcG9ydCAqIGFzIGNvZ25pdG8gZnJvbSAnYXdzLWNkay1saWIvYXdzLWNvZ25pdG8nO1xuaW1wb3J0ICogYXMgZHluYW1vZGIgZnJvbSAnYXdzLWNkay1saWIvYXdzLWR5bmFtb2RiJztcbmltcG9ydCAqIGFzIGxhbWJkYSBmcm9tICdhd3MtY2RrLWxpYi9hd3MtbGFtYmRhJztcbmltcG9ydCAqIGFzIGFwaWdhdGV3YXkgZnJvbSAnYXdzLWNkay1saWIvYXdzLWFwaWdhdGV3YXknO1xuaW1wb3J0ICogYXMgaWFtIGZyb20gJ2F3cy1jZGstbGliL2F3cy1pYW0nO1xuaW1wb3J0ICogYXMgZXZlbnRicmlkZ2UgZnJvbSAnYXdzLWNkay1saWIvYXdzLWV2ZW50cyc7XG5pbXBvcnQgKiBhcyB0YXJnZXRzIGZyb20gJ2F3cy1jZGstbGliL2F3cy1ldmVudHMtdGFyZ2V0cyc7XG5pbXBvcnQgKiBhcyBjZXJ0aWZpY2F0ZW1hbmFnZXIgZnJvbSAnYXdzLWNkay1saWIvYXdzLWNlcnRpZmljYXRlbWFuYWdlcic7XG5pbXBvcnQgKiBhcyByb3V0ZTUzIGZyb20gJ2F3cy1jZGstbGliL2F3cy1yb3V0ZTUzJztcbmltcG9ydCAqIGFzIHJvdXRlNTN0YXJnZXRzIGZyb20gJ2F3cy1jZGstbGliL2F3cy1yb3V0ZTUzLXRhcmdldHMnO1xuaW1wb3J0ICogYXMgY2xvdWR3YXRjaCBmcm9tICdhd3MtY2RrLWxpYi9hd3MtY2xvdWR3YXRjaCc7XG5pbXBvcnQgKiBhcyBjciBmcm9tICdhd3MtY2RrLWxpYi9jdXN0b20tcmVzb3VyY2VzJztcbmltcG9ydCAqIGFzIHNlY3JldHNtYW5hZ2VyIGZyb20gJ2F3cy1jZGstbGliL2F3cy1zZWNyZXRzbWFuYWdlcic7XG5cbmV4cG9ydCBpbnRlcmZhY2UgT3JnYW5pemVEQ1RlY2hTdGFja1Byb3BzIGV4dGVuZHMgY2RrLlN0YWNrUHJvcHMge1xuICBkb21haW5OYW1lPzogc3RyaW5nO1xuICBjZXJ0aWZpY2F0ZUFybj86IHN0cmluZztcbiAgaG9zdGVkWm9uZUlkPzogc3RyaW5nO1xuICBjb2duaXRvRG9tYWluUHJlZml4Pzogc3RyaW5nO1xufVxuXG5leHBvcnQgY2xhc3MgSW5mcmFzdHJ1Y3R1cmVTdGFjayBleHRlbmRzIGNkay5TdGFjayB7XG4gIGNvbnN0cnVjdG9yKHNjb3BlOiBDb25zdHJ1Y3QsIGlkOiBzdHJpbmcsIHByb3BzPzogT3JnYW5pemVEQ1RlY2hTdGFja1Byb3BzKSB7XG4gICAgc3VwZXIoc2NvcGUsIGlkLCBwcm9wcyk7XG5cbiAgICAvLyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PVxuICAgIC8vIENvZ25pdG8gVXNlciBQb29sIGZvciBBdXRoZW50aWNhdGlvblxuICAgIC8vID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09XG4gICAgY29uc3QgdXNlclBvb2wgPSBuZXcgY29nbml0by5Vc2VyUG9vbCh0aGlzLCAnT3JnYW5pemVVc2VyUG9vbCcsIHtcbiAgICAgIHVzZXJQb29sTmFtZTogJ29yZ2FuaXplLWRjdGVjaC1ldmVudHMtdXNlcnMnLFxuICAgICAgc2VsZlNpZ25VcEVuYWJsZWQ6IHRydWUsXG4gICAgICBzaWduSW5BbGlhc2VzOiB7XG4gICAgICAgIGVtYWlsOiB0cnVlLFxuICAgICAgICB1c2VybmFtZTogdHJ1ZSxcbiAgICAgIH0sXG4gICAgICBhdXRvVmVyaWZ5OiB7XG4gICAgICAgIGVtYWlsOiB0cnVlLFxuICAgICAgfSxcbiAgICAgIHN0YW5kYXJkQXR0cmlidXRlczoge1xuICAgICAgICBlbWFpbDoge1xuICAgICAgICAgIHJlcXVpcmVkOiB0cnVlLFxuICAgICAgICAgIG11dGFibGU6IHRydWUsXG4gICAgICAgIH0sXG4gICAgICAgIGZ1bGxuYW1lOiB7XG4gICAgICAgICAgcmVxdWlyZWQ6IHRydWUsXG4gICAgICAgICAgbXV0YWJsZTogdHJ1ZSxcbiAgICAgICAgfSxcbiAgICAgIH0sXG4gICAgICBjdXN0b21BdHRyaWJ1dGVzOiB7XG4gICAgICAgIGJpbzogbmV3IGNvZ25pdG8uU3RyaW5nQXR0cmlidXRlKHsgbWluTGVuOiAwLCBtYXhMZW46IDUwMCwgbXV0YWJsZTogdHJ1ZSB9KSxcbiAgICAgICAgd2Vic2l0ZTogbmV3IGNvZ25pdG8uU3RyaW5nQXR0cmlidXRlKHsgbWluTGVuOiAwLCBtYXhMZW46IDIwMCwgbXV0YWJsZTogdHJ1ZSB9KSxcbiAgICAgIH0sXG4gICAgICBwYXNzd29yZFBvbGljeToge1xuICAgICAgICBtaW5MZW5ndGg6IDgsXG4gICAgICAgIHJlcXVpcmVMb3dlcmNhc2U6IHRydWUsXG4gICAgICAgIHJlcXVpcmVVcHBlcmNhc2U6IHRydWUsXG4gICAgICAgIHJlcXVpcmVEaWdpdHM6IHRydWUsXG4gICAgICAgIHJlcXVpcmVTeW1ib2xzOiBmYWxzZSxcbiAgICAgIH0sXG4gICAgICBhY2NvdW50UmVjb3Zlcnk6IGNvZ25pdG8uQWNjb3VudFJlY292ZXJ5LkVNQUlMX09OTFksXG4gICAgICByZW1vdmFsUG9saWN5OiBjZGsuUmVtb3ZhbFBvbGljeS5SRVRBSU4sXG4gICAgfSk7XG5cbiAgICAvLyBTb2NpYWwgSWRlbnRpdHkgUHJvdmlkZXJzIChHb29nbGUsIEdpdEh1YiwgZXRjLilcbiAgICAvLyBUaGVzZSBhcmUgbWFuYWdlZCBNQU5VQUxMWSB2aWEgQVdTIENvbnNvbGUgb3IgQ0xJIC0gbm90IGJ5IENESyFcbiAgICAvLyBUaGlzIGFsbG93cyB5b3UgdG8gYWRkL3VwZGF0ZSBPQXV0aCBjcmVkZW50aWFscyB3aXRob3V0IHJlZGVwbG95aW5nIHRoZSBzdGFjay5cbiAgICAvL1xuICAgIC8vIFRvIGFkZCBhIHByb3ZpZGVyOlxuICAgIC8vIDEuIEdvIHRvIEFXUyBDb2duaXRvIENvbnNvbGUg4oaSIFVzZXIgUG9vbHMg4oaSIG9yZ2FuaXplLWRjdGVjaC1ldmVudHMtdXNlcnNcbiAgICAvLyAyLiBDbGljayBcIlNpZ24taW4gZXhwZXJpZW5jZVwiIOKGkiBcIkFkZCBpZGVudGl0eSBwcm92aWRlclwiXG4gICAgLy8gMy4gT3IgdXNlIEFXUyBDTEkgKHNlZSBTT0NJQUxfTE9HSU4ubWQgZm9yIGV4YW1wbGVzKVxuICAgIC8vXG4gICAgLy8gVGhlIFVzZXJQb29sQ2xpZW50IGJlbG93IHN1cHBvcnRzIGFsbCBpZGVudGl0eSBwcm92aWRlcnMgLSB5b3UganVzdCBuZWVkIHRvXG4gICAgLy8gdGVsbCBpdCB3aGljaCBvbmVzIGFyZSBhdmFpbGFibGUgYnkgdXBkYXRpbmcgdGhlIGxpc3QgaGVyZSB3aGVuIHlvdSBhZGQgdGhlbS5cblxuICAgIGNvbnN0IGlkZW50aXR5UHJvdmlkZXJzID0gW1xuICAgICAgY29nbml0by5Vc2VyUG9vbENsaWVudElkZW50aXR5UHJvdmlkZXIuQ09HTklUTywgIC8vIFVzZXJuYW1lL3Bhc3N3b3JkXG4gICAgICAvLyBBZGQgbW9yZSBhcyB5b3UgY3JlYXRlIHRoZW0gbWFudWFsbHk6XG4gICAgICAvLyBjb2duaXRvLlVzZXJQb29sQ2xpZW50SWRlbnRpdHlQcm92aWRlci5HT09HTEUsXG4gICAgICAvLyBjb2duaXRvLlVzZXJQb29sQ2xpZW50SWRlbnRpdHlQcm92aWRlci5jdXN0b20oJ0dpdEh1YicpLFxuICAgIF07XG5cbiAgICBjb25zdCB1c2VyUG9vbENsaWVudCA9IG5ldyBjb2duaXRvLlVzZXJQb29sQ2xpZW50KHRoaXMsICdPcmdhbml6ZVVzZXJQb29sQ2xpZW50Jywge1xuICAgICAgdXNlclBvb2wsXG4gICAgICBhdXRoRmxvd3M6IHtcbiAgICAgICAgdXNlclBhc3N3b3JkOiB0cnVlLFxuICAgICAgICB1c2VyU3JwOiB0cnVlLFxuICAgICAgfSxcbiAgICAgIGdlbmVyYXRlU2VjcmV0OiBmYWxzZSxcbiAgICAgIC8vIHN1cHBvcnRlZElkZW50aXR5UHJvdmlkZXJzIGlzIGludGVudGlvbmFsbHkgY29tbWVudGVkIG91dFxuICAgICAgLy8gVGhpcyBhbGxvd3MgQUxMIGNvbmZpZ3VyZWQgcHJvdmlkZXJzIChtYW51YWwgKyBDREstbWFuYWdlZCkgdG8gd29ya1xuICAgICAgLy8gc3VwcG9ydGVkSWRlbnRpdHlQcm92aWRlcnM6IGlkZW50aXR5UHJvdmlkZXJzLFxuICAgICAgb0F1dGg6IHtcbiAgICAgICAgZmxvd3M6IHtcbiAgICAgICAgICBhdXRob3JpemF0aW9uQ29kZUdyYW50OiB0cnVlLFxuICAgICAgICAgIGltcGxpY2l0Q29kZUdyYW50OiB0cnVlLFxuICAgICAgICB9LFxuICAgICAgICBzY29wZXM6IFtcbiAgICAgICAgICBjb2duaXRvLk9BdXRoU2NvcGUuRU1BSUwsXG4gICAgICAgICAgY29nbml0by5PQXV0aFNjb3BlLk9QRU5JRCxcbiAgICAgICAgICBjb2duaXRvLk9BdXRoU2NvcGUuUFJPRklMRSxcbiAgICAgICAgXSxcbiAgICAgICAgY2FsbGJhY2tVcmxzOiBbXG4gICAgICAgICAgJ2h0dHBzOi8vbmV4dC5kY3RlY2guZXZlbnRzL2NhbGxiYWNrJyxcbiAgICAgICAgICAnaHR0cDovL2xvY2FsaG9zdDozMDAwL2NhbGxiYWNrJyxcbiAgICAgICAgICAuLi4ocHJvcHM/LmRvbWFpbk5hbWUgPyBbYGh0dHBzOi8vJHtwcm9wcy5kb21haW5OYW1lfS9jYWxsYmFja2BdIDogW10pXG4gICAgICAgIF0sXG4gICAgICAgIGxvZ291dFVybHM6IFtcbiAgICAgICAgICAnaHR0cHM6Ly9uZXh0LmRjdGVjaC5ldmVudHMnLFxuICAgICAgICAgICdodHRwOi8vbG9jYWxob3N0OjMwMDAnLFxuICAgICAgICAgIC4uLihwcm9wcz8uZG9tYWluTmFtZSA/IFtgaHR0cHM6Ly8ke3Byb3BzLmRvbWFpbk5hbWV9YF0gOiBbXSlcbiAgICAgICAgXSxcbiAgICAgIH0sXG4gICAgfSk7XG5cbiAgICAvLyBVc2UgcHJvdmlkZWQgZG9tYWluIHByZWZpeCBvciBnZW5lcmF0ZSBvbmUgd2l0aCBhY2NvdW50IElEIGZvciB1bmlxdWVuZXNzXG4gICAgLy8gQ29nbml0byBkb21haW4gcHJlZml4ZXMgbXVzdCBiZSBnbG9iYWxseSB1bmlxdWUgYWNyb3NzIGFsbCBBV1MgcmVnaW9uc1xuICAgIGNvbnN0IGNvZ25pdG9Eb21haW5QcmVmaXggPSBwcm9wcz8uY29nbml0b0RvbWFpblByZWZpeCB8fFxuICAgICAgYG9yZ2FuaXplLWRjdGVjaC0ke2Nkay5TdGFjay5vZih0aGlzKS5hY2NvdW50fWA7XG5cbiAgICBjb25zdCB1c2VyUG9vbERvbWFpbiA9IHVzZXJQb29sLmFkZERvbWFpbignT3JnYW5pemVVc2VyUG9vbERvbWFpbicsIHtcbiAgICAgIGNvZ25pdG9Eb21haW46IHtcbiAgICAgICAgZG9tYWluUHJlZml4OiBjb2duaXRvRG9tYWluUHJlZml4LFxuICAgICAgfSxcbiAgICB9KTtcblxuICAgIC8vIENyZWF0ZSAnYWRtaW4nIGdyb3VwIGZvciBwcml2aWxlZ2VkIHVzZXJzICh0b3BpYyBjcmVhdGlvbiwgbW9kZXJhdGlvbiwgZXRjLilcbiAgICBuZXcgY29nbml0by5DZm5Vc2VyUG9vbEdyb3VwKHRoaXMsICdBZG1pbkdyb3VwJywge1xuICAgICAgdXNlclBvb2xJZDogdXNlclBvb2wudXNlclBvb2xJZCxcbiAgICAgIGdyb3VwTmFtZTogJ2FkbWluJyxcbiAgICAgIGRlc2NyaXB0aW9uOiAnQWRtaW5pc3RyYXRvcnMgd2hvIGNhbiBjcmVhdGUgdG9waWNzIGFuZCBtb2RlcmF0ZSBjb250ZW50JyxcbiAgICB9KTtcblxuICAgIC8vID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09XG4gICAgLy8gRHluYW1vREIgVGFibGVzXG4gICAgLy8gPT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT1cblxuICAgIC8vIFVzZXJzIHRhYmxlIChmb3IgZXh0ZW5kZWQgcHJvZmlsZSBpbmZvIGJleW9uZCBDb2duaXRvKVxuICAgIGNvbnN0IHVzZXJzVGFibGUgPSBuZXcgZHluYW1vZGIuVGFibGUodGhpcywgJ1VzZXJzVGFibGUnLCB7XG4gICAgICB0YWJsZU5hbWU6ICdvcmdhbml6ZS11c2VycycsXG4gICAgICBwYXJ0aXRpb25LZXk6IHsgbmFtZTogJ3VzZXJJZCcsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgICBiaWxsaW5nTW9kZTogZHluYW1vZGIuQmlsbGluZ01vZGUuUEFZX1BFUl9SRVFVRVNULFxuICAgICAgcmVtb3ZhbFBvbGljeTogY2RrLlJlbW92YWxQb2xpY3kuUkVUQUlOLFxuICAgICAgcG9pbnRJblRpbWVSZWNvdmVyeVNwZWNpZmljYXRpb246IHsgcG9pbnRJblRpbWVSZWNvdmVyeUVuYWJsZWQ6IHRydWUgfSxcbiAgICB9KTtcblxuICAgIC8vIEFkZCBHU0kgZm9yIG5pY2tuYW1lIGxvb2t1cHMgKHB1YmxpYyBwcm9maWxlIHBhZ2VzKVxuICAgIHVzZXJzVGFibGUuYWRkR2xvYmFsU2Vjb25kYXJ5SW5kZXgoe1xuICAgICAgaW5kZXhOYW1lOiAnbmlja25hbWVJbmRleCcsXG4gICAgICBwYXJ0aXRpb25LZXk6IHsgbmFtZTogJ25pY2tuYW1lJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICB9KTtcblxuXG4gICAgLy8gR3JvdXBzIHRhYmxlXG4gICAgY29uc3QgZ3JvdXBzVGFibGUgPSBuZXcgZHluYW1vZGIuVGFibGUodGhpcywgJ0dyb3Vwc1RhYmxlJywge1xuICAgICAgdGFibGVOYW1lOiAnb3JnYW5pemUtZ3JvdXBzJyxcbiAgICAgIHBhcnRpdGlvbktleTogeyBuYW1lOiAnZ3JvdXBJZCcsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgICBiaWxsaW5nTW9kZTogZHluYW1vZGIuQmlsbGluZ01vZGUuUEFZX1BFUl9SRVFVRVNULFxuICAgICAgcmVtb3ZhbFBvbGljeTogY2RrLlJlbW92YWxQb2xpY3kuUkVUQUlOLFxuICAgICAgcG9pbnRJblRpbWVSZWNvdmVyeVNwZWNpZmljYXRpb246IHsgcG9pbnRJblRpbWVSZWNvdmVyeUVuYWJsZWQ6IHRydWUgfSxcbiAgICB9KTtcblxuICAgIC8vIEFkZCBHU0kgZm9yIGFjdGl2ZSBncm91cHNcbiAgICBncm91cHNUYWJsZS5hZGRHbG9iYWxTZWNvbmRhcnlJbmRleCh7XG4gICAgICBpbmRleE5hbWU6ICdhY3RpdmVHcm91cHNJbmRleCcsXG4gICAgICBwYXJ0aXRpb25LZXk6IHsgbmFtZTogJ2FjdGl2ZScsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgICBzb3J0S2V5OiB7IG5hbWU6ICdjcmVhdGVkQXQnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLlNUUklORyB9LFxuICAgIH0pO1xuXG4gICAgLy8gR3JvdXAgTWVtYmVycyB0YWJsZVxuICAgIGNvbnN0IGdyb3VwTWVtYmVyc1RhYmxlID0gbmV3IGR5bmFtb2RiLlRhYmxlKHRoaXMsICdHcm91cE1lbWJlcnNUYWJsZScsIHtcbiAgICAgIHRhYmxlTmFtZTogJ29yZ2FuaXplLWdyb3VwLW1lbWJlcnMnLFxuICAgICAgcGFydGl0aW9uS2V5OiB7IG5hbWU6ICdncm91cElkJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICAgIHNvcnRLZXk6IHsgbmFtZTogJ3VzZXJJZCcsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgICBiaWxsaW5nTW9kZTogZHluYW1vZGIuQmlsbGluZ01vZGUuUEFZX1BFUl9SRVFVRVNULFxuICAgICAgcmVtb3ZhbFBvbGljeTogY2RrLlJlbW92YWxQb2xpY3kuUkVUQUlOLFxuICAgICAgcG9pbnRJblRpbWVSZWNvdmVyeVNwZWNpZmljYXRpb246IHsgcG9pbnRJblRpbWVSZWNvdmVyeUVuYWJsZWQ6IHRydWUgfSxcbiAgICB9KTtcblxuICAgIC8vIEFkZCBHU0kgZm9yIHVzZXIncyBncm91cHNcbiAgICBncm91cE1lbWJlcnNUYWJsZS5hZGRHbG9iYWxTZWNvbmRhcnlJbmRleCh7XG4gICAgICBpbmRleE5hbWU6ICd1c2VyR3JvdXBzSW5kZXgnLFxuICAgICAgcGFydGl0aW9uS2V5OiB7IG5hbWU6ICd1c2VySWQnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLlNUUklORyB9LFxuICAgICAgc29ydEtleTogeyBuYW1lOiAnZ3JvdXBJZCcsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgfSk7XG5cbiAgICAvLyBFdmVudHMgdGFibGVcbiAgICBjb25zdCBldmVudHNUYWJsZSA9IG5ldyBkeW5hbW9kYi5UYWJsZSh0aGlzLCAnRXZlbnRzVGFibGUnLCB7XG4gICAgICB0YWJsZU5hbWU6ICdvcmdhbml6ZS1ldmVudHMnLFxuICAgICAgcGFydGl0aW9uS2V5OiB7IG5hbWU6ICdldmVudElkJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICAgIGJpbGxpbmdNb2RlOiBkeW5hbW9kYi5CaWxsaW5nTW9kZS5QQVlfUEVSX1JFUVVFU1QsXG4gICAgICByZW1vdmFsUG9saWN5OiBjZGsuUmVtb3ZhbFBvbGljeS5SRVRBSU4sXG4gICAgICBwb2ludEluVGltZVJlY292ZXJ5U3BlY2lmaWNhdGlvbjogeyBwb2ludEluVGltZVJlY292ZXJ5RW5hYmxlZDogdHJ1ZSB9LFxuICAgIH0pO1xuXG4gICAgLy8gQWRkIEdTSSBmb3IgZXZlbnRzIGJ5IGdyb3VwXG4gICAgZXZlbnRzVGFibGUuYWRkR2xvYmFsU2Vjb25kYXJ5SW5kZXgoe1xuICAgICAgaW5kZXhOYW1lOiAnZ3JvdXBFdmVudHNJbmRleCcsXG4gICAgICBwYXJ0aXRpb25LZXk6IHsgbmFtZTogJ2dyb3VwSWQnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLlNUUklORyB9LFxuICAgICAgc29ydEtleTogeyBuYW1lOiAnZXZlbnREYXRlJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICB9KTtcblxuICAgIC8vIEFkZCBHU0kgZm9yIGV2ZW50cyBieSBkYXRlXG4gICAgZXZlbnRzVGFibGUuYWRkR2xvYmFsU2Vjb25kYXJ5SW5kZXgoe1xuICAgICAgaW5kZXhOYW1lOiAnZGF0ZUV2ZW50c0luZGV4JyxcbiAgICAgIHBhcnRpdGlvbktleTogeyBuYW1lOiAnZXZlbnRUeXBlJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICAgIHNvcnRLZXk6IHsgbmFtZTogJ2V2ZW50RGF0ZScsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgfSk7XG5cbiAgICAvLyBSU1ZQcyB0YWJsZVxuICAgIGNvbnN0IHJzdnBzVGFibGUgPSBuZXcgZHluYW1vZGIuVGFibGUodGhpcywgJ1JTVlBzVGFibGUnLCB7XG4gICAgICB0YWJsZU5hbWU6ICdvcmdhbml6ZS1yc3ZwcycsXG4gICAgICBwYXJ0aXRpb25LZXk6IHsgbmFtZTogJ2V2ZW50SWQnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLlNUUklORyB9LFxuICAgICAgc29ydEtleTogeyBuYW1lOiAndXNlcklkJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICAgIGJpbGxpbmdNb2RlOiBkeW5hbW9kYi5CaWxsaW5nTW9kZS5QQVlfUEVSX1JFUVVFU1QsXG4gICAgICByZW1vdmFsUG9saWN5OiBjZGsuUmVtb3ZhbFBvbGljeS5SRVRBSU4sXG4gICAgICBwb2ludEluVGltZVJlY292ZXJ5U3BlY2lmaWNhdGlvbjogeyBwb2ludEluVGltZVJlY292ZXJ5RW5hYmxlZDogdHJ1ZSB9LFxuICAgIH0pO1xuXG4gICAgLy8gQWRkIEdTSSBmb3IgdXNlcidzIFJTVlBzXG4gICAgcnN2cHNUYWJsZS5hZGRHbG9iYWxTZWNvbmRhcnlJbmRleCh7XG4gICAgICBpbmRleE5hbWU6ICd1c2VyUlNWUHNJbmRleCcsXG4gICAgICBwYXJ0aXRpb25LZXk6IHsgbmFtZTogJ3VzZXJJZCcsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgICBzb3J0S2V5OiB7IG5hbWU6ICdldmVudElkJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICB9KTtcblxuICAgIC8vIE1lc3NhZ2VzIHRhYmxlXG4gICAgY29uc3QgbWVzc2FnZXNUYWJsZSA9IG5ldyBkeW5hbW9kYi5UYWJsZSh0aGlzLCAnTWVzc2FnZXNUYWJsZScsIHtcbiAgICAgIHRhYmxlTmFtZTogJ29yZ2FuaXplLW1lc3NhZ2VzJyxcbiAgICAgIHBhcnRpdGlvbktleTogeyBuYW1lOiAnZ3JvdXBJZCcsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgICBzb3J0S2V5OiB7IG5hbWU6ICd0aW1lc3RhbXAnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLlNUUklORyB9LFxuICAgICAgYmlsbGluZ01vZGU6IGR5bmFtb2RiLkJpbGxpbmdNb2RlLlBBWV9QRVJfUkVRVUVTVCxcbiAgICAgIHJlbW92YWxQb2xpY3k6IGNkay5SZW1vdmFsUG9saWN5LlJFVEFJTixcbiAgICAgIHBvaW50SW5UaW1lUmVjb3ZlcnlTcGVjaWZpY2F0aW9uOiB7IHBvaW50SW5UaW1lUmVjb3ZlcnlFbmFibGVkOiB0cnVlIH0sXG4gICAgfSk7XG5cbiAgICAvLyBUb3BpY3MgdGFibGUgKGZvciBjb21tdW5pdHkgaHViIGNhdGVnb3JpZXMpXG4gICAgY29uc3QgdG9waWNzVGFibGUgPSBuZXcgZHluYW1vZGIuVGFibGUodGhpcywgJ1RvcGljc1RhYmxlJywge1xuICAgICAgdGFibGVOYW1lOiAnb3JnYW5pemUtdG9waWNzJyxcbiAgICAgIHBhcnRpdGlvbktleTogeyBuYW1lOiAnc2x1ZycsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgICBiaWxsaW5nTW9kZTogZHluYW1vZGIuQmlsbGluZ01vZGUuUEFZX1BFUl9SRVFVRVNULFxuICAgICAgcmVtb3ZhbFBvbGljeTogY2RrLlJlbW92YWxQb2xpY3kuUkVUQUlOLFxuICAgICAgcG9pbnRJblRpbWVSZWNvdmVyeVNwZWNpZmljYXRpb246IHsgcG9pbnRJblRpbWVSZWNvdmVyeUVuYWJsZWQ6IHRydWUgfSxcbiAgICB9KTtcblxuICAgIC8vIEFkZCBHU0kgZm9yIGdyb3VwcyBieSB0b3BpY1xuICAgIGdyb3Vwc1RhYmxlLmFkZEdsb2JhbFNlY29uZGFyeUluZGV4KHtcbiAgICAgIGluZGV4TmFtZTogJ3RvcGljSW5kZXgnLFxuICAgICAgcGFydGl0aW9uS2V5OiB7IG5hbWU6ICd0b3BpY1NsdWcnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLlNUUklORyB9LFxuICAgICAgc29ydEtleTogeyBuYW1lOiAnbmFtZScsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgfSk7XG5cbiAgICAvLyBBZGQgR1NJIGZvciBldmVudHMgYnkgdG9waWNcbiAgICBldmVudHNUYWJsZS5hZGRHbG9iYWxTZWNvbmRhcnlJbmRleCh7XG4gICAgICBpbmRleE5hbWU6ICd0b3BpY0luZGV4JyxcbiAgICAgIHBhcnRpdGlvbktleTogeyBuYW1lOiAndG9waWNTbHVnJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICAgIHNvcnRLZXk6IHsgbmFtZTogJ2V2ZW50RGF0ZScsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgfSk7XG5cbiAgICAvLyBUb3BpY0ZvbGxvd3MgdGFibGUgKHVzZXIgdG9waWMgc3Vic2NyaXB0aW9ucylcbiAgICBjb25zdCB0b3BpY0ZvbGxvd3NUYWJsZSA9IG5ldyBkeW5hbW9kYi5UYWJsZSh0aGlzLCAnVG9waWNGb2xsb3dzVGFibGUnLCB7XG4gICAgICB0YWJsZU5hbWU6ICdvcmdhbml6ZS10b3BpYy1mb2xsb3dzJyxcbiAgICAgIHBhcnRpdGlvbktleTogeyBuYW1lOiAndXNlcklkJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICAgIHNvcnRLZXk6IHsgbmFtZTogJ3RvcGljU2x1ZycsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgICBiaWxsaW5nTW9kZTogZHluYW1vZGIuQmlsbGluZ01vZGUuUEFZX1BFUl9SRVFVRVNULFxuICAgICAgcmVtb3ZhbFBvbGljeTogY2RrLlJlbW92YWxQb2xpY3kuUkVUQUlOLFxuICAgICAgcG9pbnRJblRpbWVSZWNvdmVyeVNwZWNpZmljYXRpb246IHsgcG9pbnRJblRpbWVSZWNvdmVyeUVuYWJsZWQ6IHRydWUgfSxcbiAgICB9KTtcblxuICAgIC8vIEFkZCBHU0kgZm9yIGdldHRpbmcgZm9sbG93ZXJzIG9mIGEgdG9waWNcbiAgICB0b3BpY0ZvbGxvd3NUYWJsZS5hZGRHbG9iYWxTZWNvbmRhcnlJbmRleCh7XG4gICAgICBpbmRleE5hbWU6ICd0b3BpY0ZvbGxvd2Vyc0luZGV4JyxcbiAgICAgIHBhcnRpdGlvbktleTogeyBuYW1lOiAndG9waWNTbHVnJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICAgIHNvcnRLZXk6IHsgbmFtZTogJ3VzZXJJZCcsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgfSk7XG5cbiAgICAvLyBFdmVudFVwdm90ZXMgdGFibGUgKGZvciB0cmFja2luZyBldmVudCB1cHZvdGVzKVxuICAgIGNvbnN0IGV2ZW50VXB2b3Rlc1RhYmxlID0gbmV3IGR5bmFtb2RiLlRhYmxlKHRoaXMsICdFdmVudFVwdm90ZXNUYWJsZScsIHtcbiAgICAgIHRhYmxlTmFtZTogJ29yZ2FuaXplLWV2ZW50LXVwdm90ZXMnLFxuICAgICAgcGFydGl0aW9uS2V5OiB7IG5hbWU6ICdldmVudElkJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICAgIHNvcnRLZXk6IHsgbmFtZTogJ3VzZXJJZCcsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgICBiaWxsaW5nTW9kZTogZHluYW1vZGIuQmlsbGluZ01vZGUuUEFZX1BFUl9SRVFVRVNULFxuICAgICAgcmVtb3ZhbFBvbGljeTogY2RrLlJlbW92YWxQb2xpY3kuUkVUQUlOLFxuICAgICAgcG9pbnRJblRpbWVSZWNvdmVyeVNwZWNpZmljYXRpb246IHsgcG9pbnRJblRpbWVSZWNvdmVyeUVuYWJsZWQ6IHRydWUgfSxcbiAgICB9KTtcblxuICAgIC8vIEFkZCBHU0kgZm9yIGdldHRpbmcgYWxsIHVwdm90ZXMgYnkgYSB1c2VyXG4gICAgZXZlbnRVcHZvdGVzVGFibGUuYWRkR2xvYmFsU2Vjb25kYXJ5SW5kZXgoe1xuICAgICAgaW5kZXhOYW1lOiAndXNlclVwdm90ZXNJbmRleCcsXG4gICAgICBwYXJ0aXRpb25LZXk6IHsgbmFtZTogJ3VzZXJJZCcsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgICBzb3J0S2V5OiB7IG5hbWU6ICdldmVudElkJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICB9KTtcblxuICAgIC8vID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09XG4gICAgLy8gUGhhc2UgNzogRGlzY3Vzc2lvbiBCb2FyZHMgVGFibGVzXG4gICAgLy8gPT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT1cblxuICAgIC8vIFRocmVhZHMgdGFibGUgKGZvciB0b3BpYyBkaXNjdXNzaW9ucylcbiAgICBjb25zdCB0aHJlYWRzVGFibGUgPSBuZXcgZHluYW1vZGIuVGFibGUodGhpcywgJ1RocmVhZHNUYWJsZScsIHtcbiAgICAgIHRhYmxlTmFtZTogJ29yZ2FuaXplLXRocmVhZHMnLFxuICAgICAgcGFydGl0aW9uS2V5OiB7IG5hbWU6ICd0b3BpY1NsdWcnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLlNUUklORyB9LFxuICAgICAgc29ydEtleTogeyBuYW1lOiAndGhyZWFkSWQnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLlNUUklORyB9LFxuICAgICAgYmlsbGluZ01vZGU6IGR5bmFtb2RiLkJpbGxpbmdNb2RlLlBBWV9QRVJfUkVRVUVTVCxcbiAgICAgIHJlbW92YWxQb2xpY3k6IGNkay5SZW1vdmFsUG9saWN5LlJFVEFJTixcbiAgICAgIHBvaW50SW5UaW1lUmVjb3ZlcnlTcGVjaWZpY2F0aW9uOiB7IHBvaW50SW5UaW1lUmVjb3ZlcnlFbmFibGVkOiB0cnVlIH0sXG4gICAgfSk7XG5cbiAgICAvLyBBZGQgR1NJIGZvciBnZXR0aW5nIHRocmVhZHMgYnkgY3JlYXRpb24gZGF0ZSAoZm9yIFwiTmV3XCIgc29ydGluZylcbiAgICB0aHJlYWRzVGFibGUuYWRkR2xvYmFsU2Vjb25kYXJ5SW5kZXgoe1xuICAgICAgaW5kZXhOYW1lOiAndGhyZWFkc0J5RGF0ZUluZGV4JyxcbiAgICAgIHBhcnRpdGlvbktleTogeyBuYW1lOiAndG9waWNTbHVnJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICAgIHNvcnRLZXk6IHsgbmFtZTogJ2NyZWF0ZWRBdCcsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgfSk7XG5cbiAgICAvLyBBZGQgR1NJIGZvciBnZXR0aW5nIHRocmVhZHMgYnkgc2NvcmUgKGZvciBcIkhvdFwiIHNvcnRpbmcpXG4gICAgdGhyZWFkc1RhYmxlLmFkZEdsb2JhbFNlY29uZGFyeUluZGV4KHtcbiAgICAgIGluZGV4TmFtZTogJ3RocmVhZHNCeVNjb3JlSW5kZXgnLFxuICAgICAgcGFydGl0aW9uS2V5OiB7IG5hbWU6ICd0b3BpY1NsdWcnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLlNUUklORyB9LFxuICAgICAgc29ydEtleTogeyBuYW1lOiAnc2NvcmUnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLk5VTUJFUiB9LFxuICAgIH0pO1xuXG4gICAgLy8gUmVwbGllcyB0YWJsZSAoZm9yIHRocmVhZCBjb21tZW50cylcbiAgICBjb25zdCByZXBsaWVzVGFibGUgPSBuZXcgZHluYW1vZGIuVGFibGUodGhpcywgJ1JlcGxpZXNUYWJsZScsIHtcbiAgICAgIHRhYmxlTmFtZTogJ29yZ2FuaXplLXJlcGxpZXMnLFxuICAgICAgcGFydGl0aW9uS2V5OiB7IG5hbWU6ICd0aHJlYWRJZCcsIHR5cGU6IGR5bmFtb2RiLkF0dHJpYnV0ZVR5cGUuU1RSSU5HIH0sXG4gICAgICBzb3J0S2V5OiB7IG5hbWU6ICdyZXBseUlkJywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICAgIGJpbGxpbmdNb2RlOiBkeW5hbW9kYi5CaWxsaW5nTW9kZS5QQVlfUEVSX1JFUVVFU1QsXG4gICAgICByZW1vdmFsUG9saWN5OiBjZGsuUmVtb3ZhbFBvbGljeS5SRVRBSU4sXG4gICAgICBwb2ludEluVGltZVJlY292ZXJ5U3BlY2lmaWNhdGlvbjogeyBwb2ludEluVGltZVJlY292ZXJ5RW5hYmxlZDogdHJ1ZSB9LFxuICAgIH0pO1xuXG4gICAgLy8gQWRkIEdTSSBmb3IgZ2V0dGluZyByZXBsaWVzIGJ5IHBhcmVudCAoZm9yIG5lc3RlZCB0aHJlYWRpbmcpXG4gICAgcmVwbGllc1RhYmxlLmFkZEdsb2JhbFNlY29uZGFyeUluZGV4KHtcbiAgICAgIGluZGV4TmFtZTogJ3JlcGxpZXNCeVBhcmVudEluZGV4JyxcbiAgICAgIHBhcnRpdGlvbktleTogeyBuYW1lOiAncGFyZW50SWQnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLlNUUklORyB9LFxuICAgICAgc29ydEtleTogeyBuYW1lOiAnY3JlYXRlZEF0JywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICB9KTtcblxuICAgIC8vID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09XG4gICAgLy8gUGhhc2UgODogTW9kZXJhdGlvbiBUYWJsZXNcbiAgICAvLyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PVxuXG4gICAgLy8gRmxhZ3MgdGFibGUgKGZvciBjb250ZW50IG1vZGVyYXRpb24pXG4gICAgY29uc3QgZmxhZ3NUYWJsZSA9IG5ldyBkeW5hbW9kYi5UYWJsZSh0aGlzLCAnRmxhZ3NUYWJsZScsIHtcbiAgICAgIHRhYmxlTmFtZTogJ29yZ2FuaXplLWZsYWdzJyxcbiAgICAgIHBhcnRpdGlvbktleTogeyBuYW1lOiAndGFyZ2V0S2V5JywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSwgLy8gRm9ybWF0OiB0YXJnZXRUeXBlI3RhcmdldElkXG4gICAgICBzb3J0S2V5OiB7IG5hbWU6ICdmbGFnSWQnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLlNUUklORyB9LFxuICAgICAgYmlsbGluZ01vZGU6IGR5bmFtb2RiLkJpbGxpbmdNb2RlLlBBWV9QRVJfUkVRVUVTVCxcbiAgICAgIHJlbW92YWxQb2xpY3k6IGNkay5SZW1vdmFsUG9saWN5LlJFVEFJTixcbiAgICAgIHBvaW50SW5UaW1lUmVjb3ZlcnlTcGVjaWZpY2F0aW9uOiB7IHBvaW50SW5UaW1lUmVjb3ZlcnlFbmFibGVkOiB0cnVlIH0sXG4gICAgfSk7XG5cbiAgICAvLyBBZGQgR1NJIGZvciBnZXR0aW5nIHBlbmRpbmcgZmxhZ3MgKGZvciBtb2RlcmF0aW9uIHF1ZXVlKVxuICAgIGZsYWdzVGFibGUuYWRkR2xvYmFsU2Vjb25kYXJ5SW5kZXgoe1xuICAgICAgaW5kZXhOYW1lOiAncGVuZGluZ0ZsYWdzSW5kZXgnLFxuICAgICAgcGFydGl0aW9uS2V5OiB7IG5hbWU6ICdzdGF0dXMnLCB0eXBlOiBkeW5hbW9kYi5BdHRyaWJ1dGVUeXBlLlNUUklORyB9LFxuICAgICAgc29ydEtleTogeyBuYW1lOiAnY3JlYXRlZEF0JywgdHlwZTogZHluYW1vZGIuQXR0cmlidXRlVHlwZS5TVFJJTkcgfSxcbiAgICB9KTtcblxuXG4gICAgLy8gPT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT1cbiAgICAvLyBTMyBCdWNrZXQgZm9yIFN0YXRpYyBXZWJzaXRlIGFuZCBFeHBvcnRzXG4gICAgLy8gPT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT1cbiAgICBjb25zdCB3ZWJzaXRlQnVja2V0ID0gbmV3IHMzLkJ1Y2tldCh0aGlzLCAnT3JnYW5pemVXZWJzaXRlQnVja2V0Jywge1xuICAgICAgd2Vic2l0ZUluZGV4RG9jdW1lbnQ6ICdpbmRleC5odG1sJyxcbiAgICAgIHdlYnNpdGVFcnJvckRvY3VtZW50OiAnaW5kZXguaHRtbCcsXG4gICAgICBwdWJsaWNSZWFkQWNjZXNzOiBmYWxzZSxcbiAgICAgIGJsb2NrUHVibGljQWNjZXNzOiBzMy5CbG9ja1B1YmxpY0FjY2Vzcy5CTE9DS19BTEwsXG4gICAgICByZW1vdmFsUG9saWN5OiBjZGsuUmVtb3ZhbFBvbGljeS5SRVRBSU4sXG4gICAgICBjb3JzOiBbXG4gICAgICAgIHtcbiAgICAgICAgICBhbGxvd2VkT3JpZ2luczogWycqJ10sXG4gICAgICAgICAgYWxsb3dlZE1ldGhvZHM6IFtzMy5IdHRwTWV0aG9kcy5HRVRdLFxuICAgICAgICAgIGFsbG93ZWRIZWFkZXJzOiBbJyonXSxcbiAgICAgICAgfSxcbiAgICAgIF0sXG4gICAgfSk7XG5cbiAgICAvLyBPcmlnaW4gQWNjZXNzIENvbnRyb2wgZm9yIENsb3VkRnJvbnQgdG8gYWNjZXNzIFMzXG4gICAgY29uc3Qgb3JpZ2luQWNjZXNzQ29udHJvbCA9IG5ldyBjbG91ZGZyb250LlMzT3JpZ2luQWNjZXNzQ29udHJvbCh0aGlzLCAnTmV4dE9BQycsIHtcbiAgICAgIGRlc2NyaXB0aW9uOiAnT0FDIGZvciBuZXh0LmRjdGVjaC5ldmVudHMgc3RhdGljIGFzc2V0cycsXG4gICAgfSk7XG5cbiAgICAvLyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PVxuICAgIC8vIFJvdXRlIDUzIGFuZCBTU0wgQ2VydGlmaWNhdGVcbiAgICAvLyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PVxuXG4gICAgLy8gTG9vayB1cCB0aGUgZXhpc3RpbmcgaG9zdGVkIHpvbmUgZm9yIGRjdGVjaC5ldmVudHNcbiAgICBjb25zdCBob3N0ZWRab25lID0gcm91dGU1My5Ib3N0ZWRab25lLmZyb21Mb29rdXAodGhpcywgJ0RjVGVjaEhvc3RlZFpvbmUnLCB7XG4gICAgICBkb21haW5OYW1lOiAnZGN0ZWNoLmV2ZW50cycsXG4gICAgfSk7XG5cbiAgICAvLyBDcmVhdGUgQUNNIGNlcnRpZmljYXRlIGZvciBuZXh0LmRjdGVjaC5ldmVudHMgKG11c3QgYmUgaW4gdXMtZWFzdC0xIGZvciBDbG91ZEZyb250KVxuICAgIGxldCBjZXJ0aWZpY2F0ZTogY2VydGlmaWNhdGVtYW5hZ2VyLklDZXJ0aWZpY2F0ZTtcbiAgICBpZiAoY2RrLlN0YWNrLm9mKHRoaXMpLnJlZ2lvbiAhPT0gJ3VzLWVhc3QtMScpIHtcbiAgICAgIHRocm93IG5ldyBFcnJvcihcbiAgICAgICAgJ0FDTSBjZXJ0aWZpY2F0ZXMgZm9yIENsb3VkRnJvbnQgbXVzdCBiZSBjcmVhdGVkIGluIHVzLWVhc3QtMS4gJyArXG4gICAgICAgICdEZXBsb3kgdGhpcyBzdGFjayB0byB1cy1lYXN0LTEuJ1xuICAgICAgKTtcbiAgICB9XG4gICAgY2VydGlmaWNhdGUgPSBuZXcgY2VydGlmaWNhdGVtYW5hZ2VyLkNlcnRpZmljYXRlKHRoaXMsICdOZXh0RGNUZWNoQ2VydGlmaWNhdGUnLCB7XG4gICAgICBkb21haW5OYW1lOiAnbmV4dC5kY3RlY2guZXZlbnRzJyxcbiAgICAgIHZhbGlkYXRpb246IGNlcnRpZmljYXRlbWFuYWdlci5DZXJ0aWZpY2F0ZVZhbGlkYXRpb24uZnJvbURucyhob3N0ZWRab25lKSxcbiAgICB9KTtcblxuICAgIC8vID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09XG4gICAgLy8gQ2xvdWRGcm9udCBEaXN0cmlidXRpb24gZm9yIG5leHQuZGN0ZWNoLmV2ZW50c1xuICAgIC8vID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09XG5cbiAgICAvLyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PVxuICAgIC8vIExhbWJkYSBGdW5jdGlvbnMgZm9yIEFQSVxuICAgIC8vID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09XG5cbiAgICAvLyBFbnZpcm9ubWVudCB2YXJpYWJsZXMgZm9yIExhbWJkYSBmdW5jdGlvbnNcbiAgICBjb25zdCBsYW1iZGFFbnYgPSB7XG4gICAgICBVU0VSU19UQUJMRTogdXNlcnNUYWJsZS50YWJsZU5hbWUsXG4gICAgICBHUk9VUFNfVEFCTEU6IGdyb3Vwc1RhYmxlLnRhYmxlTmFtZSxcbiAgICAgIEdST1VQX01FTUJFUlNfVEFCTEU6IGdyb3VwTWVtYmVyc1RhYmxlLnRhYmxlTmFtZSxcbiAgICAgIEVWRU5UU19UQUJMRTogZXZlbnRzVGFibGUudGFibGVOYW1lLFxuICAgICAgUlNWUFNfVEFCTEU6IHJzdnBzVGFibGUudGFibGVOYW1lLFxuICAgICAgTUVTU0FHRVNfVEFCTEU6IG1lc3NhZ2VzVGFibGUudGFibGVOYW1lLFxuICAgICAgVE9QSUNTX1RBQkxFOiB0b3BpY3NUYWJsZS50YWJsZU5hbWUsXG4gICAgICBUT1BJQ19GT0xMT1dTX1RBQkxFOiB0b3BpY0ZvbGxvd3NUYWJsZS50YWJsZU5hbWUsXG4gICAgICBFVkVOVF9VUFZPVEVTX1RBQkxFOiBldmVudFVwdm90ZXNUYWJsZS50YWJsZU5hbWUsXG4gICAgICBUSFJFQURTX1RBQkxFOiB0aHJlYWRzVGFibGUudGFibGVOYW1lLFxuICAgICAgUkVQTElFU19UQUJMRTogcmVwbGllc1RhYmxlLnRhYmxlTmFtZSxcbiAgICAgIEZMQUdTX1RBQkxFOiBmbGFnc1RhYmxlLnRhYmxlTmFtZSxcbiAgICAgIFVTRVJfUE9PTF9JRDogdXNlclBvb2wudXNlclBvb2xJZCxcbiAgICAgIFVTRVJfUE9PTF9DTElFTlRfSUQ6IHVzZXJQb29sQ2xpZW50LnVzZXJQb29sQ2xpZW50SWQsXG4gICAgICBVU0VSX1BPT0xfUkVHSU9OOiBjZGsuU3RhY2sub2YodGhpcykucmVnaW9uLFxuICAgICAgQ09HTklUT19ET01BSU46IHVzZXJQb29sRG9tYWluLmRvbWFpbk5hbWUsXG4gICAgICBXRUJTSVRFX0JVQ0tFVDogd2Vic2l0ZUJ1Y2tldC5idWNrZXROYW1lLFxuICAgICAgTkVYVF9EQ1RFQ0hfRE9NQUlOOiB0aGlzLm5vZGUudHJ5R2V0Q29udGV4dCgnbmV4dERvbWFpbicpIHx8ICduZXh0LmRjdGVjaC5ldmVudHMnLFxuICAgICAgR0lUSFVCX1JFUE86IHRoaXMubm9kZS50cnlHZXRDb250ZXh0KCdnaXRodWJSZXBvJykgfHwgJ3Jvc3NrYXJjaG5lci9kY3RlY2guZXZlbnRzJyxcbiAgICAgIC8vIFBoYXNlIDk6IEVtYWlsIE5vdGlmaWNhdGlvbnNcbiAgICAgIFNFU19TT1VSQ0VfRU1BSUw6ICdvdXRnb2luZ0BkY3RlY2guZXZlbnRzJyxcbiAgICB9O1xuXG5cblxuICAgIC8vIExhbWJkYSBMYXllciBmb3IgSGFuZGxlYmFycyB0ZW1wbGF0ZXNcbiAgICBjb25zdCB0ZW1wbGF0ZXNMYXllciA9IG5ldyBsYW1iZGEuTGF5ZXJWZXJzaW9uKHRoaXMsICdUZW1wbGF0ZXNMYXllcicsIHtcbiAgICAgIGNvZGU6IGxhbWJkYS5Db2RlLmZyb21Bc3NldCgnbGFtYmRhL2xheWVycy90ZW1wbGF0ZXMnKSxcbiAgICAgIGNvbXBhdGlibGVSdW50aW1lczogW2xhbWJkYS5SdW50aW1lLk5PREVKU18yMF9YXSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnSGFuZGxlYmFycyB0ZW1wbGF0ZXMgZm9yIGJvdGggb3JnYW5pemUgYW5kIG5leHQuZGN0ZWNoLmV2ZW50cycsXG4gICAgfSk7XG5cbiAgICAvLyBBUEkgTGFtYmRhIGZ1bmN0aW9uIChoYW5kbGVzIGFsbCBBUEkgcm91dGVzKVxuICAgIGNvbnN0IGFwaUZ1bmN0aW9uID0gbmV3IGxhbWJkYS5GdW5jdGlvbih0aGlzLCAnQXBpRnVuY3Rpb24nLCB7XG4gICAgICBydW50aW1lOiBsYW1iZGEuUnVudGltZS5OT0RFSlNfMjBfWCxcbiAgICAgIGhhbmRsZXI6ICdpbmRleC5oYW5kbGVyJyxcbiAgICAgIGNvZGU6IGxhbWJkYS5Db2RlLmZyb21Bc3NldCgnbGFtYmRhL2FwaScpLFxuICAgICAgZW52aXJvbm1lbnQ6IGxhbWJkYUVudixcbiAgICAgIHRpbWVvdXQ6IGNkay5EdXJhdGlvbi5zZWNvbmRzKDMwKSxcbiAgICAgIG1lbW9yeVNpemU6IDUxMixcbiAgICAgIHRyYWNpbmc6IGxhbWJkYS5UcmFjaW5nLkFDVElWRSwgLy8gRW5hYmxlIFgtUmF5IHRyYWNpbmdcbiAgICAgIGxheWVyczogW3RlbXBsYXRlc0xheWVyXSxcbiAgICB9KTtcblxuICAgIC8vIEdyYW50IHBlcm1pc3Npb25zIHRvIExhbWJkYVxuICAgIHVzZXJzVGFibGUuZ3JhbnRSZWFkV3JpdGVEYXRhKGFwaUZ1bmN0aW9uKTtcbiAgICBncm91cHNUYWJsZS5ncmFudFJlYWRXcml0ZURhdGEoYXBpRnVuY3Rpb24pO1xuICAgIGdyb3VwTWVtYmVyc1RhYmxlLmdyYW50UmVhZFdyaXRlRGF0YShhcGlGdW5jdGlvbik7XG4gICAgZXZlbnRzVGFibGUuZ3JhbnRSZWFkV3JpdGVEYXRhKGFwaUZ1bmN0aW9uKTtcbiAgICByc3Zwc1RhYmxlLmdyYW50UmVhZFdyaXRlRGF0YShhcGlGdW5jdGlvbik7XG4gICAgbWVzc2FnZXNUYWJsZS5ncmFudFJlYWRXcml0ZURhdGEoYXBpRnVuY3Rpb24pO1xuICAgIHRvcGljc1RhYmxlLmdyYW50UmVhZFdyaXRlRGF0YShhcGlGdW5jdGlvbik7XG4gICAgdG9waWNGb2xsb3dzVGFibGUuZ3JhbnRSZWFkV3JpdGVEYXRhKGFwaUZ1bmN0aW9uKTtcbiAgICBldmVudFVwdm90ZXNUYWJsZS5ncmFudFJlYWRXcml0ZURhdGEoYXBpRnVuY3Rpb24pO1xuICAgIHRocmVhZHNUYWJsZS5ncmFudFJlYWRXcml0ZURhdGEoYXBpRnVuY3Rpb24pO1xuICAgIHJlcGxpZXNUYWJsZS5ncmFudFJlYWRXcml0ZURhdGEoYXBpRnVuY3Rpb24pO1xuICAgIGZsYWdzVGFibGUuZ3JhbnRSZWFkV3JpdGVEYXRhKGFwaUZ1bmN0aW9uKTtcblxuICAgIC8vIFBoYXNlIDk6IEdyYW50IFNFUyBwZXJtaXNzaW9ucyBmb3IgZW1haWwgbm90aWZpY2F0aW9uc1xuICAgIGFwaUZ1bmN0aW9uLmFkZFRvUm9sZVBvbGljeShcbiAgICAgIG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgICAgYWN0aW9uczogWydzZXM6U2VuZEVtYWlsJywgJ3NlczpTZW5kUmF3RW1haWwnXSxcbiAgICAgICAgcmVzb3VyY2VzOiBbXG4gICAgICAgICAgYGFybjphd3M6c2VzOiR7dGhpcy5yZWdpb259OiR7dGhpcy5hY2NvdW50fTppZGVudGl0eS9kY3RlY2guZXZlbnRzYCxcbiAgICAgICAgICBgYXJuOmF3czpzZXM6JHt0aGlzLnJlZ2lvbn06JHt0aGlzLmFjY291bnR9OmlkZW50aXR5L291dGdvaW5nQGRjdGVjaC5ldmVudHNgLFxuICAgICAgICBdLFxuICAgICAgfSlcbiAgICApO1xuXG5cbiAgICAvLyBHcmFudCBDb2duaXRvIHBlcm1pc3Npb25zXG4gICAgYXBpRnVuY3Rpb24uYWRkVG9Sb2xlUG9saWN5KFxuICAgICAgbmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgICBhY3Rpb25zOiBbJ2NvZ25pdG8taWRwOkdldFVzZXInLCAnY29nbml0by1pZHA6QWRtaW5HZXRVc2VyJywgJ2NvZ25pdG8taWRwOkFkbWluTGlzdEdyb3Vwc0ZvclVzZXInXSxcbiAgICAgICAgcmVzb3VyY2VzOiBbdXNlclBvb2wudXNlclBvb2xBcm5dLFxuICAgICAgfSlcbiAgICApO1xuXG4gICAgLy8gRXhwb3J0IExhbWJkYSBmdW5jdGlvbiAoZ2VuZXJhdGVzIGdyb3Vwcy55YW1sIGFuZCBldmVudHMueWFtbClcbiAgICBjb25zdCBleHBvcnRGdW5jdGlvbiA9IG5ldyBsYW1iZGEuRnVuY3Rpb24odGhpcywgJ0V4cG9ydEZ1bmN0aW9uJywge1xuICAgICAgcnVudGltZTogbGFtYmRhLlJ1bnRpbWUuTk9ERUpTXzIwX1gsXG4gICAgICBoYW5kbGVyOiAnaW5kZXguaGFuZGxlcicsXG4gICAgICBjb2RlOiBsYW1iZGEuQ29kZS5mcm9tQXNzZXQoJ2xhbWJkYS9leHBvcnQnKSxcbiAgICAgIGVudmlyb25tZW50OiBsYW1iZGFFbnYsXG4gICAgICB0aW1lb3V0OiBjZGsuRHVyYXRpb24ubWludXRlcyg1KSxcbiAgICAgIG1lbW9yeVNpemU6IDEwMjQsXG4gICAgICB0cmFjaW5nOiBsYW1iZGEuVHJhY2luZy5BQ1RJVkUsIC8vIEVuYWJsZSBYLVJheSB0cmFjaW5nXG4gICAgfSk7XG5cbiAgICAvLyBHcmFudCBwZXJtaXNzaW9ucyB0byBleHBvcnQgTGFtYmRhXG4gICAgZ3JvdXBzVGFibGUuZ3JhbnRSZWFkRGF0YShleHBvcnRGdW5jdGlvbik7XG4gICAgZXZlbnRzVGFibGUuZ3JhbnRSZWFkRGF0YShleHBvcnRGdW5jdGlvbik7XG4gICAgd2Vic2l0ZUJ1Y2tldC5ncmFudFdyaXRlKGV4cG9ydEZ1bmN0aW9uKTtcblxuICAgIC8vIFNjaGVkdWxlIGV4cG9ydCB0byBydW4gZXZlcnkgNSBtaW51dGVzXG4gICAgY29uc3QgZXhwb3J0UnVsZSA9IG5ldyBldmVudGJyaWRnZS5SdWxlKHRoaXMsICdFeHBvcnRTY2hlZHVsZScsIHtcbiAgICAgIHNjaGVkdWxlOiBldmVudGJyaWRnZS5TY2hlZHVsZS5yYXRlKGNkay5EdXJhdGlvbi5taW51dGVzKDUpKSxcbiAgICB9KTtcbiAgICBleHBvcnRSdWxlLmFkZFRhcmdldChuZXcgdGFyZ2V0cy5MYW1iZGFGdW5jdGlvbihleHBvcnRGdW5jdGlvbikpO1xuXG4gICAgLy8gUmVmcmVzaCBMYW1iZGEgZnVuY3Rpb24gKHN5bmNzIGdyb3Vwcy9ldmVudHMgZnJvbSBHaXRIdWIgYW5kIGZldGNoZXMgaUNhbCBmZWVkcylcbiAgICBjb25zdCByZWZyZXNoRnVuY3Rpb24gPSBuZXcgbGFtYmRhLkRvY2tlckltYWdlRnVuY3Rpb24odGhpcywgJ1JlZnJlc2hGdW5jdGlvbicsIHtcbiAgICAgIGNvZGU6IGxhbWJkYS5Eb2NrZXJJbWFnZUNvZGUuZnJvbUltYWdlQXNzZXQoJ2xhbWJkYS9yZWZyZXNoJyksXG4gICAgICBlbnZpcm9ubWVudDoge1xuICAgICAgICAuLi5sYW1iZGFFbnYsXG4gICAgICAgIEdJVEhVQl9UT0tFTl9TRUNSRVQ6ICdkY3RlY2gtZXZlbnRzL2dpdGh1Yi10b2tlbicsXG4gICAgICB9LFxuICAgICAgdGltZW91dDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoNSksXG4gICAgICBtZW1vcnlTaXplOiAxMDI0LFxuICAgICAgZGVzY3JpcHRpb246ICdGZXRjaGVzIGlDYWwgZmVlZHMgYW5kIHN5bmNzIGZyb20gR2l0SHViIHJlcG8nLFxuICAgICAgdHJhY2luZzogbGFtYmRhLlRyYWNpbmcuQUNUSVZFLFxuICAgIH0pO1xuXG4gICAgLy8gR3JhbnQgcGVybWlzc2lvbnMgdG8gcmVmcmVzaCBMYW1iZGFcbiAgICBncm91cHNUYWJsZS5ncmFudFJlYWRXcml0ZURhdGEocmVmcmVzaEZ1bmN0aW9uKTtcbiAgICBldmVudHNUYWJsZS5ncmFudFJlYWRXcml0ZURhdGEocmVmcmVzaEZ1bmN0aW9uKTtcblxuICAgIC8vIEdyYW50IHBlcm1pc3Npb24gdG8gcmVhZCBHaXRIdWIgdG9rZW4gZnJvbSBTZWNyZXRzIE1hbmFnZXJcbiAgICByZWZyZXNoRnVuY3Rpb24uYWRkVG9Sb2xlUG9saWN5KFxuICAgICAgbmV3IGlhbS5Qb2xpY3lTdGF0ZW1lbnQoe1xuICAgICAgICBhY3Rpb25zOiBbJ3NlY3JldHNtYW5hZ2VyOkdldFNlY3JldFZhbHVlJ10sXG4gICAgICAgIHJlc291cmNlczogW2Bhcm46YXdzOnNlY3JldHNtYW5hZ2VyOiR7dGhpcy5yZWdpb259OiR7dGhpcy5hY2NvdW50fTpzZWNyZXQ6ZGN0ZWNoLWV2ZW50cy9naXRodWItdG9rZW4tKmBdLFxuICAgICAgfSlcbiAgICApO1xuXG4gICAgLy8gU2NoZWR1bGUgcmVmcmVzaCB0byBydW4gZXZlcnkgNCBob3Vyc1xuICAgIGNvbnN0IHJlZnJlc2hSdWxlID0gbmV3IGV2ZW50YnJpZGdlLlJ1bGUodGhpcywgJ1JlZnJlc2hTY2hlZHVsZScsIHtcbiAgICAgIHNjaGVkdWxlOiBldmVudGJyaWRnZS5TY2hlZHVsZS5yYXRlKGNkay5EdXJhdGlvbi5ob3Vycyg0KSksXG4gICAgICBkZXNjcmlwdGlvbjogJ1JlZnJlc2ggaUNhbCBmZWVkcyBmcm9tIDExMCsgZ3JvdXBzIGFuZCBzeW5jIGZyb20gR2l0SHViJyxcbiAgICB9KTtcbiAgICByZWZyZXNoUnVsZS5hZGRUYXJnZXQobmV3IHRhcmdldHMuTGFtYmRhRnVuY3Rpb24ocmVmcmVzaEZ1bmN0aW9uKSk7XG5cbiAgICAvLyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PVxuICAgIC8vIFBoYXNlIDY6IFJlY3VycmVuY2UgRXhwYW5zaW9uIExhbWJkYVxuICAgIC8vID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09XG5cbiAgICAvLyBSZWN1cnJlbmNlIExhbWJkYSAoZXhwYW5kcyByZWN1cnJpbmcgZXZlbnRzIGludG8gaW5zdGFuY2VzKVxuICAgIGNvbnN0IHJlY3VycmVuY2VGdW5jdGlvbiA9IG5ldyBsYW1iZGEuRnVuY3Rpb24odGhpcywgJ1JlY3VycmVuY2VGdW5jdGlvbicsIHtcbiAgICAgIHJ1bnRpbWU6IGxhbWJkYS5SdW50aW1lLk5PREVKU18yMF9YLFxuICAgICAgaGFuZGxlcjogJ2luZGV4LmhhbmRsZXInLFxuICAgICAgY29kZTogbGFtYmRhLkNvZGUuZnJvbUFzc2V0KCdsYW1iZGEvcmVjdXJyZW5jZScpLFxuICAgICAgZW52aXJvbm1lbnQ6IHtcbiAgICAgICAgRVZFTlRTX1RBQkxFOiBldmVudHNUYWJsZS50YWJsZU5hbWUsXG4gICAgICB9LFxuICAgICAgdGltZW91dDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoMiksXG4gICAgICBtZW1vcnlTaXplOiAyNTYsXG4gICAgICBkZXNjcmlwdGlvbjogJ0V4cGFuZHMgcmVjdXJyaW5nIGV2ZW50cyBpbnRvIGluZGl2aWR1YWwgaW5zdGFuY2VzJyxcbiAgICB9KTtcblxuICAgIC8vIEdyYW50IHBlcm1pc3Npb25zIHRvIHJlY3VycmVuY2UgTGFtYmRhXG4gICAgZXZlbnRzVGFibGUuZ3JhbnRSZWFkV3JpdGVEYXRhKHJlY3VycmVuY2VGdW5jdGlvbik7XG5cbiAgICAvLyBTY2hlZHVsZSByZWN1cnJlbmNlIGV4cGFuc2lvbiB0byBydW4gZGFpbHkgYXQgMmFtIFVUQ1xuICAgIGNvbnN0IHJlY3VycmVuY2VSdWxlID0gbmV3IGV2ZW50YnJpZGdlLlJ1bGUodGhpcywgJ1JlY3VycmVuY2VTY2hlZHVsZScsIHtcbiAgICAgIHNjaGVkdWxlOiBldmVudGJyaWRnZS5TY2hlZHVsZS5jcm9uKHsgaG91cjogJzInLCBtaW51dGU6ICcwJyB9KSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnRGFpbHkgZXhwYW5zaW9uIG9mIHJlY3VycmluZyBldmVudHMnLFxuICAgIH0pO1xuICAgIHJlY3VycmVuY2VSdWxlLmFkZFRhcmdldChuZXcgdGFyZ2V0cy5MYW1iZGFGdW5jdGlvbihyZWN1cnJlbmNlRnVuY3Rpb24pKTtcblxuICAgIC8vID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09XG4gICAgLy8gQ2xvdWRXYXRjaCBBbGFybXMgZm9yIE1vbml0b3JpbmdcbiAgICAvLyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PVxuXG4gICAgLy8gQVBJIExhbWJkYSBlcnJvciBhbGFybVxuICAgIGNvbnN0IGFwaUVycm9yQWxhcm0gPSBuZXcgY2xvdWR3YXRjaC5BbGFybSh0aGlzLCAnQXBpTGFtYmRhRXJyb3JBbGFybScsIHtcbiAgICAgIG1ldHJpYzogYXBpRnVuY3Rpb24ubWV0cmljRXJyb3JzKHtcbiAgICAgICAgc3RhdGlzdGljOiAnU3VtJyxcbiAgICAgICAgcGVyaW9kOiBjZGsuRHVyYXRpb24ubWludXRlcyg1KSxcbiAgICAgIH0pLFxuICAgICAgdGhyZXNob2xkOiAxMCxcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiAxLFxuICAgICAgYWxhcm1EZXNjcmlwdGlvbjogJ0FsZXJ0IHdoZW4gQVBJIExhbWJkYSBmdW5jdGlvbiBoYXMgbW9yZSB0aGFuIDEwIGVycm9ycyBpbiA1IG1pbnV0ZXMnLFxuICAgICAgYWxhcm1OYW1lOiAnb3JnYW5pemUtYXBpLWxhbWJkYS1lcnJvcnMnLFxuICAgICAgdHJlYXRNaXNzaW5nRGF0YTogY2xvdWR3YXRjaC5UcmVhdE1pc3NpbmdEYXRhLk5PVF9CUkVBQ0hJTkcsXG4gICAgfSk7XG5cbiAgICAvLyBFeHBvcnQgTGFtYmRhIGVycm9yIGFsYXJtXG4gICAgY29uc3QgZXhwb3J0RXJyb3JBbGFybSA9IG5ldyBjbG91ZHdhdGNoLkFsYXJtKHRoaXMsICdFeHBvcnRMYW1iZGFFcnJvckFsYXJtJywge1xuICAgICAgbWV0cmljOiBleHBvcnRGdW5jdGlvbi5tZXRyaWNFcnJvcnMoe1xuICAgICAgICBzdGF0aXN0aWM6ICdTdW0nLFxuICAgICAgICBwZXJpb2Q6IGNkay5EdXJhdGlvbi5taW51dGVzKDUpLFxuICAgICAgfSksXG4gICAgICB0aHJlc2hvbGQ6IDMsXG4gICAgICBldmFsdWF0aW9uUGVyaW9kczogMixcbiAgICAgIGFsYXJtRGVzY3JpcHRpb246ICdBbGVydCB3aGVuIEV4cG9ydCBMYW1iZGEgZnVuY3Rpb24gaGFzIG1vcmUgdGhhbiAzIGVycm9ycyBpbiAxMCBtaW51dGVzJyxcbiAgICAgIGFsYXJtTmFtZTogJ29yZ2FuaXplLWV4cG9ydC1sYW1iZGEtZXJyb3JzJyxcbiAgICAgIHRyZWF0TWlzc2luZ0RhdGE6IGNsb3Vkd2F0Y2guVHJlYXRNaXNzaW5nRGF0YS5OT1RfQlJFQUNISU5HLFxuICAgIH0pO1xuXG4gICAgLy8gRHluYW1vREIgdGhyb3R0bGUgYWxhcm1zIGZvciBjcml0aWNhbCB0YWJsZXNcbiAgICBjb25zdCBncm91cHNUaHJvdHRsZUFsYXJtID0gbmV3IGNsb3Vkd2F0Y2guQWxhcm0odGhpcywgJ0dyb3Vwc1RhYmxlVGhyb3R0bGVBbGFybScsIHtcbiAgICAgIG1ldHJpYzogZ3JvdXBzVGFibGUubWV0cmljVXNlckVycm9ycyh7XG4gICAgICAgIHN0YXRpc3RpYzogJ1N1bScsXG4gICAgICAgIHBlcmlvZDogY2RrLkR1cmF0aW9uLm1pbnV0ZXMoNSksXG4gICAgICB9KSxcbiAgICAgIHRocmVzaG9sZDogNSxcbiAgICAgIGV2YWx1YXRpb25QZXJpb2RzOiAyLFxuICAgICAgYWxhcm1EZXNjcmlwdGlvbjogJ0FsZXJ0IHdoZW4gR3JvdXBzIHRhYmxlIGV4cGVyaWVuY2VzIHRocm90dGxpbmcnLFxuICAgICAgYWxhcm1OYW1lOiAnb3JnYW5pemUtZ3JvdXBzLXRhYmxlLXRocm90dGxlJyxcbiAgICAgIHRyZWF0TWlzc2luZ0RhdGE6IGNsb3Vkd2F0Y2guVHJlYXRNaXNzaW5nRGF0YS5OT1RfQlJFQUNISU5HLFxuICAgIH0pO1xuXG4gICAgY29uc3QgZXZlbnRzVGhyb3R0bGVBbGFybSA9IG5ldyBjbG91ZHdhdGNoLkFsYXJtKHRoaXMsICdFdmVudHNUYWJsZVRocm90dGxlQWxhcm0nLCB7XG4gICAgICBtZXRyaWM6IGV2ZW50c1RhYmxlLm1ldHJpY1VzZXJFcnJvcnMoe1xuICAgICAgICBzdGF0aXN0aWM6ICdTdW0nLFxuICAgICAgICBwZXJpb2Q6IGNkay5EdXJhdGlvbi5taW51dGVzKDUpLFxuICAgICAgfSksXG4gICAgICB0aHJlc2hvbGQ6IDUsXG4gICAgICBldmFsdWF0aW9uUGVyaW9kczogMixcbiAgICAgIGFsYXJtRGVzY3JpcHRpb246ICdBbGVydCB3aGVuIEV2ZW50cyB0YWJsZSBleHBlcmllbmNlcyB0aHJvdHRsaW5nJyxcbiAgICAgIGFsYXJtTmFtZTogJ29yZ2FuaXplLWV2ZW50cy10YWJsZS10aHJvdHRsZScsXG4gICAgICB0cmVhdE1pc3NpbmdEYXRhOiBjbG91ZHdhdGNoLlRyZWF0TWlzc2luZ0RhdGEuTk9UX0JSRUFDSElORyxcbiAgICB9KTtcblxuICAgIC8vID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09XG4gICAgLy8gQVBJIEdhdGV3YXlcbiAgICAvLyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PVxuICAgIGNvbnN0IGFwaSA9IG5ldyBhcGlnYXRld2F5LlJlc3RBcGkodGhpcywgJ09yZ2FuaXplQXBpJywge1xuICAgICAgcmVzdEFwaU5hbWU6ICdPcmdhbml6ZSBEQyBUZWNoIEV2ZW50cyBBUEknLFxuICAgICAgZGVzY3JpcHRpb246ICdBUEkgZm9yIG9yZ2FuaXplLmRjdGVjaC5ldmVudHMnLFxuICAgICAgZGVmYXVsdENvcnNQcmVmbGlnaHRPcHRpb25zOiB7XG4gICAgICAgIGFsbG93T3JpZ2luczogW1xuICAgICAgICAgICdodHRwczovL25leHQuZGN0ZWNoLmV2ZW50cycsXG4gICAgICAgICAgJ2h0dHA6Ly9sb2NhbGhvc3Q6MzAwMCcsIC8vIEZvciBsb2NhbCBkZXZlbG9wbWVudFxuICAgICAgICBdLFxuICAgICAgICBhbGxvd01ldGhvZHM6IGFwaWdhdGV3YXkuQ29ycy5BTExfTUVUSE9EUyxcbiAgICAgICAgYWxsb3dIZWFkZXJzOiBbXG4gICAgICAgICAgJ0NvbnRlbnQtVHlwZScsXG4gICAgICAgICAgJ1gtQW16LURhdGUnLFxuICAgICAgICAgICdBdXRob3JpemF0aW9uJyxcbiAgICAgICAgICAnWC1BcGktS2V5JyxcbiAgICAgICAgICAnWC1BbXotU2VjdXJpdHktVG9rZW4nLFxuICAgICAgICAgICdIWC1SZXF1ZXN0JyxcbiAgICAgICAgICAnSFgtVGFyZ2V0JyxcbiAgICAgICAgICAnSFgtVHJpZ2dlcicsXG4gICAgICAgIF0sXG4gICAgICB9LFxuICAgIH0pO1xuXG4gICAgLy8gTGFtYmRhIGludGVncmF0aW9uXG4gICAgY29uc3QgYXBpSW50ZWdyYXRpb24gPSBuZXcgYXBpZ2F0ZXdheS5MYW1iZGFJbnRlZ3JhdGlvbihhcGlGdW5jdGlvbik7XG5cbiAgICAvLyBVc2UgcHJveHkgcmVzb3VyY2UgdG8gYXZvaWQgTGFtYmRhIHBlcm1pc3Npb24gcG9saWN5IHNpemUgbGltaXRzXG4gICAgLy8gVGhpcyBjcmVhdGVzIGEgc2luZ2xlIHBlcm1pc3Npb24gaW5zdGVhZCBvZiBvbmUgcGVyIHJvdXRlXG4gICAgLy8gVGhlIExhbWJkYSBmdW5jdGlvbiBoYW5kbGVzIGFsbCByb3V0aW5nIGFuZCBhdXRoZW50aWNhdGlvbiBpbnRlcm5hbGx5XG4gICAgLy8gTm90ZTogQXV0aG9yaXplciBpcyByZW1vdmVkIHRvIHN1cHBvcnQgYm90aCBwdWJsaWMgYW5kIHByb3RlY3RlZCByb3V0ZXNcbiAgICAvLyBUaGUgTGFtYmRhIHZhbGlkYXRlcyBDb2duaXRvIHRva2VucyB3aGVuIHByZXNlbnQgYW5kIGNoZWNrcyBwZXJtaXNzaW9ucyBwZXIgcm91dGVcblxuICAgIC8vIEFkZCByb290IHBhdGggaGFuZGxlclxuICAgIGFwaS5yb290LmFkZE1ldGhvZCgnQU5ZJywgYXBpSW50ZWdyYXRpb24pO1xuXG4gICAgLy8gQWRkIHByb3h5IHJlc291cmNlIGZvciBhbGwgb3RoZXIgcGF0aHNcbiAgICBjb25zdCBwcm94eSA9IGFwaS5yb290LmFkZFJlc291cmNlKCd7cHJveHkrfScpO1xuICAgIHByb3h5LmFkZE1ldGhvZCgnQU5ZJywgYXBpSW50ZWdyYXRpb24pO1xuXG4gICAgLy8gPT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT1cbiAgICAvLyBDbG91ZEZyb250IERpc3RyaWJ1dGlvbiBmb3IgbmV4dC5kY3RlY2guZXZlbnRzXG4gICAgLy8gPT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT1cblxuICAgIC8vIEltcG9ydCBleGlzdGluZyBXZWIgQUNMIChjcmVhdGVkIGJ5IENsb3VkRnJvbnQgcHJpY2luZyBwbGFuIHN1YnNjcmlwdGlvbilcbiAgICBjb25zdCB3ZWJBY2xBcm4gPSAnYXJuOmF3czp3YWZ2Mjp1cy1lYXN0LTE6Nzk3NDM4Njc0MjQzOmdsb2JhbC93ZWJhY2wvQ3JlYXRlZEJ5Q2xvdWRGcm9udC0wOGY5MDBhMi81OThmMzVjNi0zMzIzLTRhNmItOWFlOS05Y2Q3M2Q5ZTdjYTAnO1xuXG4gICAgY29uc3QgZGlzdHJpYnV0aW9uID0gbmV3IGNsb3VkZnJvbnQuRGlzdHJpYnV0aW9uKHRoaXMsICdOZXh0RGNUZWNoRGlzdHJpYnV0aW9uJywge1xuICAgICAgd2ViQWNsSWQ6IHdlYkFjbEFybixcbiAgICAgIGRlZmF1bHRCZWhhdmlvcjoge1xuICAgICAgICBvcmlnaW46IG5ldyBvcmlnaW5zLkh0dHBPcmlnaW4oYCR7YXBpLnJlc3RBcGlJZH0uZXhlY3V0ZS1hcGkuJHt0aGlzLnJlZ2lvbn0uYW1hem9uYXdzLmNvbWAsIHtcbiAgICAgICAgICBvcmlnaW5QYXRoOiAnL3Byb2QnLFxuICAgICAgICAgIGN1c3RvbUhlYWRlcnM6IHtcbiAgICAgICAgICAgICdYLVNpdGUnOiAnbmV4dCcsXG4gICAgICAgICAgfSxcbiAgICAgICAgfSksXG4gICAgICAgIHZpZXdlclByb3RvY29sUG9saWN5OiBjbG91ZGZyb250LlZpZXdlclByb3RvY29sUG9saWN5LlJFRElSRUNUX1RPX0hUVFBTLFxuICAgICAgICBhbGxvd2VkTWV0aG9kczogY2xvdWRmcm9udC5BbGxvd2VkTWV0aG9kcy5BTExPV19BTEwsXG4gICAgICAgIGNhY2hlUG9saWN5OiBjbG91ZGZyb250LkNhY2hlUG9saWN5LkNBQ0hJTkdfRElTQUJMRUQsIC8vIERpc2FibGUgY2FjaGluZyBmb3IgZHluYW1pYyBjb250ZW50XG4gICAgICAgIG9yaWdpblJlcXVlc3RQb2xpY3k6IGNsb3VkZnJvbnQuT3JpZ2luUmVxdWVzdFBvbGljeS5BTExfVklFV0VSX0VYQ0VQVF9IT1NUX0hFQURFUiwgLy8gRm9yd2FyZCBxdWVyeSBzdHJpbmdzIGZvciBPQXV0aCBjYWxsYmFja1xuICAgICAgfSxcbiAgICAgIGFkZGl0aW9uYWxCZWhhdmlvcnM6IHtcbiAgICAgICAgJy9zdGF0aWMvKic6IHtcbiAgICAgICAgICBvcmlnaW46IG9yaWdpbnMuUzNCdWNrZXRPcmlnaW4ud2l0aE9yaWdpbkFjY2Vzc0NvbnRyb2wod2Vic2l0ZUJ1Y2tldCwge1xuICAgICAgICAgICAgb3JpZ2luQWNjZXNzQ29udHJvbCxcbiAgICAgICAgICB9KSxcbiAgICAgICAgICB2aWV3ZXJQcm90b2NvbFBvbGljeTogY2xvdWRmcm9udC5WaWV3ZXJQcm90b2NvbFBvbGljeS5SRURJUkVDVF9UT19IVFRQUyxcbiAgICAgICAgICBjYWNoZVBvbGljeTogY2xvdWRmcm9udC5DYWNoZVBvbGljeS5DQUNISU5HX09QVElNSVpFRCxcbiAgICAgICAgfSxcbiAgICAgIH0sXG4gICAgICBkb21haW5OYW1lczogWyduZXh0LmRjdGVjaC5ldmVudHMnXSxcbiAgICAgIGNlcnRpZmljYXRlLFxuICAgIH0pO1xuXG4gICAgLy8gR3JhbnQgQ2xvdWRGcm9udCBPQUMgcmVhZCBhY2Nlc3MgdG8gUzMgYnVja2V0IGZvciBzdGF0aWMgYXNzZXRzXG4gICAgd2Vic2l0ZUJ1Y2tldC5hZGRUb1Jlc291cmNlUG9saWN5KG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgIGVmZmVjdDogaWFtLkVmZmVjdC5BTExPVyxcbiAgICAgIHByaW5jaXBhbHM6IFtuZXcgaWFtLlNlcnZpY2VQcmluY2lwYWwoJ2Nsb3VkZnJvbnQuYW1hem9uYXdzLmNvbScpXSxcbiAgICAgIGFjdGlvbnM6IFsnczM6R2V0T2JqZWN0J10sXG4gICAgICByZXNvdXJjZXM6IFt3ZWJzaXRlQnVja2V0LmFybkZvck9iamVjdHMoJyonKV0sXG4gICAgICBjb25kaXRpb25zOiB7XG4gICAgICAgIFN0cmluZ0VxdWFsczoge1xuICAgICAgICAgICdBV1M6U291cmNlQXJuJzogYGFybjphd3M6Y2xvdWRmcm9udDo6JHt0aGlzLmFjY291bnR9OmRpc3RyaWJ1dGlvbi8ke2Rpc3RyaWJ1dGlvbi5kaXN0cmlidXRpb25JZH1gLFxuICAgICAgICB9LFxuICAgICAgfSxcbiAgICB9KSk7XG5cbiAgICAvLyBBZGQgUm91dGUgNTMgQSAmIEFBQUEgcmVjb3JkcyBmb3IgbmV4dC5kY3RlY2guZXZlbnRzXG4gICAgbmV3IHJvdXRlNTMuQVJlY29yZCh0aGlzLCAnTmV4dERjVGVjaEFSZWNvcmQnLCB7XG4gICAgICB6b25lOiBob3N0ZWRab25lLFxuICAgICAgcmVjb3JkTmFtZTogJ25leHQnLFxuICAgICAgdGFyZ2V0OiByb3V0ZTUzLlJlY29yZFRhcmdldC5mcm9tQWxpYXMoXG4gICAgICAgIG5ldyByb3V0ZTUzdGFyZ2V0cy5DbG91ZEZyb250VGFyZ2V0KGRpc3RyaWJ1dGlvbilcbiAgICAgICksXG4gICAgfSk7XG5cbiAgICBuZXcgcm91dGU1My5BYWFhUmVjb3JkKHRoaXMsICdOZXh0RGNUZWNoQWFhYVJlY29yZCcsIHtcbiAgICAgIHpvbmU6IGhvc3RlZFpvbmUsXG4gICAgICByZWNvcmROYW1lOiAnbmV4dCcsXG4gICAgICB0YXJnZXQ6IHJvdXRlNTMuUmVjb3JkVGFyZ2V0LmZyb21BbGlhcyhcbiAgICAgICAgbmV3IHJvdXRlNTN0YXJnZXRzLkNsb3VkRnJvbnRUYXJnZXQoZGlzdHJpYnV0aW9uKVxuICAgICAgKSxcbiAgICB9KTtcblxuICAgIC8vID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09XG4gICAgLy8gSUFNIFVzZXIgZm9yIEdpdEh1YiBBY3Rpb25zXG4gICAgLy8gPT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT1cblxuICAgIC8vIENyZWF0ZSBJQU0gdXNlciBmb3IgR2l0SHViIEFjdGlvbnMgdG8gZGVwbG95IHN0YXRpYyBhc3NldHNcbiAgICBjb25zdCBnaXRodWJBY3Rpb25zVXNlciA9IG5ldyBpYW0uVXNlcih0aGlzLCAnR2l0SHViQWN0aW9uc1VzZXInLCB7XG4gICAgICB1c2VyTmFtZTogJ2dpdGh1Yi1hY3Rpb25zLWZyb250ZW5kLWRlcGxveScsXG4gICAgfSk7XG5cbiAgICAvLyBDcmVhdGUgcG9saWN5IGZvciBkZXBsb3lpbmcgdG8gUzMgYW5kIGludmFsaWRhdGluZyBDbG91ZEZyb250XG4gICAgY29uc3QgZnJvbnRlbmREZXBsb3lQb2xpY3kgPSBuZXcgaWFtLlBvbGljeSh0aGlzLCAnRnJvbnRlbmREZXBsb3lQb2xpY3knLCB7XG4gICAgICBwb2xpY3lOYW1lOiAnZnJvbnRlbmQtZGVwbG95LXBvbGljeScsXG4gICAgICBzdGF0ZW1lbnRzOiBbXG4gICAgICAgIC8vIFMzIHBlcm1pc3Npb25zIHRvIHVwbG9hZCBzdGF0aWMgYXNzZXRzXG4gICAgICAgIG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgICAgICBlZmZlY3Q6IGlhbS5FZmZlY3QuQUxMT1csXG4gICAgICAgICAgYWN0aW9uczogW1xuICAgICAgICAgICAgJ3MzOlB1dE9iamVjdCcsXG4gICAgICAgICAgICAnczM6UHV0T2JqZWN0QWNsJyxcbiAgICAgICAgICAgICdzMzpEZWxldGVPYmplY3QnLFxuICAgICAgICAgICAgJ3MzOkxpc3RCdWNrZXQnLFxuICAgICAgICAgIF0sXG4gICAgICAgICAgcmVzb3VyY2VzOiBbXG4gICAgICAgICAgICB3ZWJzaXRlQnVja2V0LmJ1Y2tldEFybixcbiAgICAgICAgICAgIGAke3dlYnNpdGVCdWNrZXQuYnVja2V0QXJufS8qYCxcbiAgICAgICAgICBdLFxuICAgICAgICB9KSxcbiAgICAgICAgLy8gQ2xvdWRGcm9udCBwZXJtaXNzaW9ucyB0byBpbnZhbGlkYXRlIGNhY2hlXG4gICAgICAgIG5ldyBpYW0uUG9saWN5U3RhdGVtZW50KHtcbiAgICAgICAgICBlZmZlY3Q6IGlhbS5FZmZlY3QuQUxMT1csXG4gICAgICAgICAgYWN0aW9uczogWydjbG91ZGZyb250OkNyZWF0ZUludmFsaWRhdGlvbiddLFxuICAgICAgICAgIHJlc291cmNlczogW2Bhcm46YXdzOmNsb3VkZnJvbnQ6OiR7dGhpcy5hY2NvdW50fTpkaXN0cmlidXRpb24vJHtkaXN0cmlidXRpb24uZGlzdHJpYnV0aW9uSWR9YF0sXG4gICAgICAgIH0pLFxuICAgICAgXSxcbiAgICB9KTtcblxuICAgIC8vIEF0dGFjaCBwb2xpY3kgdG8gdXNlclxuICAgIGZyb250ZW5kRGVwbG95UG9saWN5LmF0dGFjaFRvVXNlcihnaXRodWJBY3Rpb25zVXNlcik7XG5cbiAgICAvLyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PVxuICAgIC8vIE91dHB1dHNcbiAgICAvLyA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PVxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdVc2VyUG9vbElkJywge1xuICAgICAgdmFsdWU6IHVzZXJQb29sLnVzZXJQb29sSWQsXG4gICAgICBkZXNjcmlwdGlvbjogJ0NvZ25pdG8gVXNlciBQb29sIElEJyxcbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdVc2VyUG9vbENsaWVudElkJywge1xuICAgICAgdmFsdWU6IHVzZXJQb29sQ2xpZW50LnVzZXJQb29sQ2xpZW50SWQsXG4gICAgICBkZXNjcmlwdGlvbjogJ0NvZ25pdG8gVXNlciBQb29sIENsaWVudCBJRCcsXG4gICAgfSk7XG5cbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnVXNlclBvb2xEb21haW4nLCB7XG4gICAgICB2YWx1ZTogdXNlclBvb2xEb21haW4uZG9tYWluTmFtZSxcbiAgICAgIGRlc2NyaXB0aW9uOiAnQ29nbml0byBVc2VyIFBvb2wgRG9tYWluJyxcbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdBcGlVcmwnLCB7XG4gICAgICB2YWx1ZTogYXBpLnVybCxcbiAgICAgIGRlc2NyaXB0aW9uOiAnQVBJIEdhdGV3YXkgVVJMJyxcbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdDbG91ZEZyb250VXJsJywge1xuICAgICAgdmFsdWU6IGRpc3RyaWJ1dGlvbi5kaXN0cmlidXRpb25Eb21haW5OYW1lLFxuICAgICAgZGVzY3JpcHRpb246ICdDbG91ZEZyb250IERpc3RyaWJ1dGlvbiBVUkwgZm9yIG5leHQuZGN0ZWNoLmV2ZW50cycsXG4gICAgfSk7XG5cbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnV2Vic2l0ZUJ1Y2tldE5hbWUnLCB7XG4gICAgICB2YWx1ZTogd2Vic2l0ZUJ1Y2tldC5idWNrZXROYW1lLFxuICAgICAgZGVzY3JpcHRpb246ICdTMyBXZWJzaXRlIEJ1Y2tldCBOYW1lJyxcbiAgICB9KTtcblxuICAgIG5ldyBjZGsuQ2ZuT3V0cHV0KHRoaXMsICdDbG91ZEZyb250RGlzdHJpYnV0aW9uSWQnLCB7XG4gICAgICB2YWx1ZTogZGlzdHJpYnV0aW9uLmRpc3RyaWJ1dGlvbklkLFxuICAgICAgZGVzY3JpcHRpb246ICdDbG91ZEZyb250IERpc3RyaWJ1dGlvbiBJRCAoZm9yIEdpdEh1YiBBY3Rpb25zKScsXG4gICAgfSk7XG5cbiAgICBuZXcgY2RrLkNmbk91dHB1dCh0aGlzLCAnR2l0SHViQWN0aW9uc1VzZXJOYW1lJywge1xuICAgICAgdmFsdWU6IGdpdGh1YkFjdGlvbnNVc2VyLnVzZXJOYW1lLFxuICAgICAgZGVzY3JpcHRpb246ICdJQU0gVXNlciBmb3IgR2l0SHViIEFjdGlvbnMgKGNyZWF0ZSBhY2Nlc3Mga2V5cyBmb3IgdGhpcyB1c2VyKScsXG4gICAgfSk7XG4gIH1cbn1cbiJdfQ==