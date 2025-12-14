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
const cdk = __importStar(require("aws-cdk-lib"));
const assertions_1 = require("aws-cdk-lib/assertions");
const infrastructure_stack_1 = require("../lib/infrastructure-stack");
describe('InfrastructureStack', () => {
    let app;
    let stack;
    let template;
    beforeEach(() => {
        app = new cdk.App();
        // Create stack with a certificate ARN to avoid region validation error
        stack = new infrastructure_stack_1.InfrastructureStack(app, 'TestStack', {
            env: { region: 'us-east-1', account: '123456789012' },
            certificateArn: 'arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id',
        });
        template = assertions_1.Template.fromStack(stack);
    });
    describe('Cognito User Pool', () => {
        test('creates user pool with correct configuration', () => {
            template.hasResourceProperties('AWS::Cognito::UserPool', {
                UserPoolName: 'organize-dctech-events-users',
                AutoVerifiedAttributes: ['email'],
                AliasAttributes: ['email'],
                Schema: assertions_1.Match.arrayWith([
                    assertions_1.Match.objectLike({
                        Name: 'email',
                        Required: true,
                        Mutable: true,
                    }),
                ]),
            });
        });
        test('creates user pool client with OAuth configuration', () => {
            template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
                AllowedOAuthFlows: ['implicit', 'code'],
                AllowedOAuthScopes: ['email', 'openid', 'profile'],
                GenerateSecret: false,
            });
        });
        test('creates user pool domain', () => {
            template.hasResourceProperties('AWS::Cognito::UserPoolDomain', {
                Domain: 'organize-dctech-events',
            });
        });
    });
    describe('DynamoDB Tables', () => {
        test('creates users table with correct configuration', () => {
            template.hasResourceProperties('AWS::DynamoDB::Table', {
                TableName: 'organize-users',
                KeySchema: [
                    {
                        AttributeName: 'userId',
                        KeyType: 'HASH',
                    },
                ],
                BillingMode: 'PAY_PER_REQUEST',
                PointInTimeRecoverySpecification: {
                    PointInTimeRecoveryEnabled: true,
                },
            });
        });
        test('creates groups table with active groups index', () => {
            template.hasResourceProperties('AWS::DynamoDB::Table', {
                TableName: 'organize-groups',
                GlobalSecondaryIndexes: assertions_1.Match.arrayWith([
                    assertions_1.Match.objectLike({
                        IndexName: 'activeGroupsIndex',
                        KeySchema: [
                            { AttributeName: 'active', KeyType: 'HASH' },
                            { AttributeName: 'createdAt', KeyType: 'RANGE' },
                        ],
                    }),
                ]),
            });
        });
        test('creates group members table with user groups index', () => {
            template.hasResourceProperties('AWS::DynamoDB::Table', {
                TableName: 'organize-group-members',
                GlobalSecondaryIndexes: assertions_1.Match.arrayWith([
                    assertions_1.Match.objectLike({
                        IndexName: 'userGroupsIndex',
                    }),
                ]),
            });
        });
        test('creates events table with multiple indices', () => {
            template.hasResourceProperties('AWS::DynamoDB::Table', {
                TableName: 'organize-events',
                GlobalSecondaryIndexes: assertions_1.Match.arrayWith([
                    assertions_1.Match.objectLike({ IndexName: 'groupEventsIndex' }),
                    assertions_1.Match.objectLike({ IndexName: 'dateEventsIndex' }),
                ]),
            });
        });
        test('creates rsvps table with user RSVPs index', () => {
            template.hasResourceProperties('AWS::DynamoDB::Table', {
                TableName: 'organize-rsvps',
                GlobalSecondaryIndexes: assertions_1.Match.arrayWith([
                    assertions_1.Match.objectLike({ IndexName: 'userRSVPsIndex' }),
                ]),
            });
        });
        test('creates messages table', () => {
            template.hasResourceProperties('AWS::DynamoDB::Table', {
                TableName: 'organize-messages',
                KeySchema: [
                    { AttributeName: 'groupId', KeyType: 'HASH' },
                    { AttributeName: 'timestamp', KeyType: 'RANGE' },
                ],
            });
        });
    });
    describe('S3 and CloudFront', () => {
        test('creates S3 bucket with website configuration', () => {
            template.hasResourceProperties('AWS::S3::Bucket', {
                WebsiteConfiguration: {
                    IndexDocument: 'index.html',
                    ErrorDocument: 'index.html',
                },
                PublicAccessBlockConfiguration: {
                    BlockPublicAcls: true,
                    BlockPublicPolicy: true,
                    IgnorePublicAcls: true,
                    RestrictPublicBuckets: true,
                },
                CorsConfiguration: {
                    CorsRules: [
                        {
                            AllowedMethods: ['GET'],
                            AllowedOrigins: ['*'],
                            AllowedHeaders: ['*'],
                        },
                    ],
                },
            });
        });
        test('creates CloudFront distribution with custom domain', () => {
            template.hasResourceProperties('AWS::CloudFront::Distribution', {
                DistributionConfig: assertions_1.Match.objectLike({
                    Aliases: ['organize.dctech.events'],
                    DefaultRootObject: 'index.html',
                    CustomErrorResponses: assertions_1.Match.arrayWith([
                        {
                            ErrorCode: 404,
                            ResponseCode: 200,
                            ResponsePagePath: '/index.html',
                        },
                        {
                            ErrorCode: 403,
                            ResponseCode: 200,
                            ResponsePagePath: '/index.html',
                        },
                    ]),
                }),
            });
        });
        test('creates origin access identity for CloudFront', () => {
            template.resourceCountIs('AWS::CloudFront::CloudFrontOriginAccessIdentity', 1);
        });
    });
    describe('Route 53 DNS', () => {
        test('creates A record for organize subdomain', () => {
            template.hasResourceProperties('AWS::Route53::RecordSet', {
                Name: 'organize.dctech.events.',
                Type: 'A',
            });
        });
        test('creates AAAA record for IPv6 support', () => {
            template.hasResourceProperties('AWS::Route53::RecordSet', {
                Name: 'organize.dctech.events.',
                Type: 'AAAA',
            });
        });
    });
    describe('Lambda Functions', () => {
        test('creates API Lambda function with correct runtime', () => {
            template.hasResourceProperties('AWS::Lambda::Function', {
                Runtime: 'nodejs20.x',
                Handler: 'index.handler',
                Timeout: 30,
                MemorySize: 512,
                Environment: {
                    Variables: assertions_1.Match.objectLike({
                        USERS_TABLE: assertions_1.Match.anyValue(),
                        GROUPS_TABLE: assertions_1.Match.anyValue(),
                        EVENTS_TABLE: assertions_1.Match.anyValue(),
                    }),
                },
            });
        });
        test('creates export Lambda function with longer timeout', () => {
            template.hasResourceProperties('AWS::Lambda::Function', {
                Runtime: 'nodejs20.x',
                Timeout: 300,
                MemorySize: 1024,
            });
        });
        test('grants DynamoDB read/write permissions to API Lambda', () => {
            template.hasResourceProperties('AWS::IAM::Policy', {
                PolicyDocument: {
                    Statement: assertions_1.Match.arrayWith([
                        assertions_1.Match.objectLike({
                            Action: assertions_1.Match.arrayWith([
                                'dynamodb:BatchGetItem',
                                'dynamodb:GetItem',
                                'dynamodb:Scan',
                                'dynamodb:PutItem',
                                'dynamodb:UpdateItem',
                            ]),
                        }),
                    ]),
                },
            });
        });
        test('grants Cognito permissions to API Lambda', () => {
            template.hasResourceProperties('AWS::IAM::Policy', {
                PolicyDocument: {
                    Statement: assertions_1.Match.arrayWith([
                        assertions_1.Match.objectLike({
                            Action: ['cognito-idp:GetUser', 'cognito-idp:AdminGetUser'],
                        }),
                    ]),
                },
            });
        });
    });
    describe('API Gateway', () => {
        test('creates REST API with CORS configuration', () => {
            template.hasResourceProperties('AWS::ApiGateway::RestApi', {
                Name: 'Organize DC Tech Events API',
            });
        });
        test('creates proxy resource for Lambda integration', () => {
            template.hasResourceProperties('AWS::ApiGateway::Resource', {
                PathPart: '{proxy+}',
            });
        });
        test('creates ANY method for proxy resource', () => {
            template.hasResourceProperties('AWS::ApiGateway::Method', {
                HttpMethod: 'ANY',
                AuthorizationType: 'NONE',
            });
        });
    });
    describe('EventBridge', () => {
        test('creates scheduled rule for export Lambda', () => {
            template.hasResourceProperties('AWS::Events::Rule', {
                ScheduleExpression: 'rate(5 minutes)',
            });
        });
        test('configures export Lambda as rule target', () => {
            template.hasResourceProperties('AWS::Events::Rule', {
                Targets: assertions_1.Match.arrayWith([
                    assertions_1.Match.objectLike({
                        Arn: assertions_1.Match.anyValue(),
                    }),
                ]),
            });
        });
    });
    describe('Stack Outputs', () => {
        test('exports all required outputs', () => {
            const outputs = Object.keys(template.findOutputs('*'));
            expect(outputs).toContain('UserPoolId');
            expect(outputs).toContain('UserPoolClientId');
            expect(outputs).toContain('ApiUrl');
            expect(outputs).toContain('CloudFrontUrl');
            expect(outputs).toContain('WebsiteBucketName');
        });
    });
    describe('Resource Count', () => {
        test('creates expected number of DynamoDB tables', () => {
            template.resourceCountIs('AWS::DynamoDB::Table', 6);
        });
        test('creates expected number of Lambda functions', () => {
            template.resourceCountIs('AWS::Lambda::Function', 2);
        });
        test('creates single API Gateway', () => {
            template.resourceCountIs('AWS::ApiGateway::RestApi', 1);
        });
        test('creates single CloudFront distribution', () => {
            template.resourceCountIs('AWS::CloudFront::Distribution', 1);
        });
    });
});
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiaW5mcmFzdHJ1Y3R1cmUudGVzdC5qcyIsInNvdXJjZVJvb3QiOiIiLCJzb3VyY2VzIjpbImluZnJhc3RydWN0dXJlLnRlc3QudHMiXSwibmFtZXMiOltdLCJtYXBwaW5ncyI6Ijs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7QUFBQSxpREFBbUM7QUFDbkMsdURBQXlEO0FBQ3pELHNFQUFrRTtBQUVsRSxRQUFRLENBQUMscUJBQXFCLEVBQUUsR0FBRyxFQUFFO0lBQ25DLElBQUksR0FBWSxDQUFDO0lBQ2pCLElBQUksS0FBMEIsQ0FBQztJQUMvQixJQUFJLFFBQWtCLENBQUM7SUFFdkIsVUFBVSxDQUFDLEdBQUcsRUFBRTtRQUNkLEdBQUcsR0FBRyxJQUFJLEdBQUcsQ0FBQyxHQUFHLEVBQUUsQ0FBQztRQUNwQix1RUFBdUU7UUFDdkUsS0FBSyxHQUFHLElBQUksMENBQW1CLENBQUMsR0FBRyxFQUFFLFdBQVcsRUFBRTtZQUNoRCxHQUFHLEVBQUUsRUFBRSxNQUFNLEVBQUUsV0FBVyxFQUFFLE9BQU8sRUFBRSxjQUFjLEVBQUU7WUFDckQsY0FBYyxFQUFFLDZEQUE2RDtTQUM5RSxDQUFDLENBQUM7UUFDSCxRQUFRLEdBQUcscUJBQVEsQ0FBQyxTQUFTLENBQUMsS0FBSyxDQUFDLENBQUM7SUFDdkMsQ0FBQyxDQUFDLENBQUM7SUFFSCxRQUFRLENBQUMsbUJBQW1CLEVBQUUsR0FBRyxFQUFFO1FBQ2pDLElBQUksQ0FBQyw4Q0FBOEMsRUFBRSxHQUFHLEVBQUU7WUFDeEQsUUFBUSxDQUFDLHFCQUFxQixDQUFDLHdCQUF3QixFQUFFO2dCQUN2RCxZQUFZLEVBQUUsOEJBQThCO2dCQUM1QyxzQkFBc0IsRUFBRSxDQUFDLE9BQU8sQ0FBQztnQkFDakMsZUFBZSxFQUFFLENBQUMsT0FBTyxDQUFDO2dCQUMxQixNQUFNLEVBQUUsa0JBQUssQ0FBQyxTQUFTLENBQUM7b0JBQ3RCLGtCQUFLLENBQUMsVUFBVSxDQUFDO3dCQUNmLElBQUksRUFBRSxPQUFPO3dC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LENBQUMsQ0FBQztJQUNMLENBQUMsQ0FBQyxDQUFDO0lBRUgsUUFBUSxDQUFDLGdCQUFnQixFQUFFLEdBQUcsRUFBRTtRQUM5QixJQUFJLENBQUMsNENBQTRDLEVBQUUsR0FBRyxFQUFFO1lBQ3RELFFBQVEsQ0FBQyxlQUFlLENBQUMsc0JBQXNCLEVBQUUsQ0FBQyxDQUFDLENBQUM7UUFDdEQsQ0FBQyxDQUFDLENBQUM7UUFFSCxJQUFJLENBQUMsNkNBQTZDLEVBQUUsR0FBRyxFQUFFO1lBQ3ZELFFBQVEsQ0FBQyxlQUFlLENBQUMsdUJBQXVCLEVBQUUsQ0FBQyxDQUFDLENBQUM7UUFDdkQsQ0FBQyxDQUFDLENBQUM7UUFFSCxJQUFJLENBQUMsNEJBQTRCLEVBQUUsR0FBRyxFQUFFO1lBQ3RDLFFBQVEsQ0FBQyxlQUFlLENBQUMsMEJBQTBCLEVBQUUsQ0FBQyxDQUFDLENBQUM7UUFDMUQsQ0FBQyxDQUFDLENBQUM7UUFFSCxJQUFJLENBQUMsd0NBQXdDLEVBQUUsR0FBRyxFQUFFO1lBQ2xELFFBQVEsQ0FBQyxlQUFlLENBQUMsK0JBQStCLEVBQUUsQ0FBQyxDQUFDLENBQUM7UUFDL0QsQ0FBQyxDQUFDLENBQUM7SUFDTCxDQUFDLENBQUMsQ0FBQztBQUNMLENBQUMsQ0FBQyxDQUFDIiwic291cmNlc0NvbnRlbnQiOlsiaW1wb3J0ICogYXMgY2RrIGZyb20gJ2F3cy1jZGstbGliJztcbmltcG9ydCB7IFRlbXBsYXRlLCBNYXRjaCB9IGZyb20gJ2F3cy1jZGstbGliL2Fzc2VydGlvbnMnO1xuaW1wb3J0IHsgSW5mcmFzdHJ1Y3R1cmVTdGFjayB9IGZyb20gJy4uL2xpYi9pbmZyYXN0cnVjdHVyZS1zdGFjayc7XG5cbmRlc2NyaWJlKCdJbmZyYXN0cnVjdHVyZVN0YWNrJywgKCkgPT4ge1xuICBsZXQgYXBwOiBjZGsuQXBwO1xuICBsZXQgc3RhY2s6IEluZnJhc3RydWN0dXJlU3RhY2s7XG4gIGxldCB0ZW1wbGF0ZTogVGVtcGxhdGU7XG5cbiAgYmVmb3JlRWFjaCgoKSA9PiB7XG4gICAgYXBwID0gbmV3IGNkay5BcHAoKTtcbiAgICAvLyBDcmVhdGUgc3RhY2sgd2l0aCBhIGNlcnRpZmljYXRlIEFSTiB0byBhdm9pZCByZWdpb24gdmFsaWRhdGlvbiBlcnJvclxuICAgIHN0YWNrID0gbmV3IEluZnJhc3RydWN0dXJlU3RhY2soYXBwLCAnVGVzdFN0YWNrJywge1xuICAgICAgZW52OiB7IHJlZ2lvbjogJ3VzLWVhc3QtMScsIGFjY291bnQ6ICcxMjM0NTY3ODkwMTInIH0sXG4gICAgICBjZXJ0aWZpY2F0ZUFybjogJ2Fybjphd3M6YWNtOnVzLWVhc3QtMToxMjM0NTY3ODkwMTI6Y2VydGlmaWNhdGUvdGVzdC1jZXJ0LWlkJyxcbiAgICB9KTtcbiAgICB0ZW1wbGF0ZSA9IFRlbXBsYXRlLmZyb21TdGFjayhzdGFjayk7XG4gIH0pO1xuXG4gIGRlc2NyaWJlKCdDb2duaXRvIFVzZXIgUG9vbCcsICgpID0+IHtcbiAgICB0ZXN0KCdjcmVhdGVzIHVzZXIgcG9vbCB3aXRoIGNvcnJlY3QgY29uZmlndXJhdGlvbicsICgpID0+IHtcbiAgICAgIHRlbXBsYXRlLmhhc1Jlc291cmNlUHJvcGVydGllcygnQVdTOjpDb2duaXRvOjpVc2VyUG9vbCcsIHtcbiAgICAgICAgVXNlclBvb2xOYW1lOiAnb3JnYW5pemUtZGN0ZWNoLWV2ZW50cy11c2VycycsXG4gICAgICAgIEF1dG9WZXJpZmllZEF0dHJpYnV0ZXM6IFsnZW1haWwnXSxcbiAgICAgICAgQWxpYXNBdHRyaWJ1dGVzOiBbJ2VtYWlsJ10sXG4gICAgICAgIFNjaGVtYTogTWF0Y2guYXJyYXlXaXRoKFtcbiAgICAgICAgICBNYXRjaC5vYmplY3RMaWtlKHtcbiAgICAgICAgICAgIE5hbWU6ICdlbWFpbCcsXG4gICAgICAgICAgICBSZXF1aXJlZDogdHJ1ZSxcbiAgICAgICAgICAgIE11dGFibGU6IHRydWUsXG4gICAgICAgICAgfSksXG4gICAgICAgIF0pLFxuICAgICAgfSk7XG4gICAgfSk7XG5cbiAgICB0ZXN0KCdjcmVhdGVzIHVzZXIgcG9vbCBjbGllbnQgd2l0aCBPQXV0aCBjb25maWd1cmF0aW9uJywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OkNvZ25pdG86OlVzZXJQb29sQ2xpZW50Jywge1xuICAgICAgICBBbGxvd2VkT0F1dGhGbG93czogWydpbXBsaWNpdCcsICdjb2RlJ10sXG4gICAgICAgIEFsbG93ZWRPQXV0aFNjb3BlczogWydlbWFpbCcsICdvcGVuaWQnLCAncHJvZmlsZSddLFxuICAgICAgICBHZW5lcmF0ZVNlY3JldDogZmFsc2UsXG4gICAgICB9KTtcbiAgICB9KTtcblxuICAgIHRlc3QoJ2NyZWF0ZXMgdXNlciBwb29sIGRvbWFpbicsICgpID0+IHtcbiAgICAgIHRlbXBsYXRlLmhhc1Jlc291cmNlUHJvcGVydGllcygnQVdTOjpDb2duaXRvOjpVc2VyUG9vbERvbWFpbicsIHtcbiAgICAgICAgRG9tYWluOiAnb3JnYW5pemUtZGN0ZWNoLWV2ZW50cycsXG4gICAgICB9KTtcbiAgICB9KTtcbiAgfSk7XG5cbiAgZGVzY3JpYmUoJ0R5bmFtb0RCIFRhYmxlcycsICgpID0+IHtcbiAgICB0ZXN0KCdjcmVhdGVzIHVzZXJzIHRhYmxlIHdpdGggY29ycmVjdCBjb25maWd1cmF0aW9uJywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OkR5bmFtb0RCOjpUYWJsZScsIHtcbiAgICAgICAgVGFibGVOYW1lOiAnb3JnYW5pemUtdXNlcnMnLFxuICAgICAgICBLZXlTY2hlbWE6IFtcbiAgICAgICAgICB7XG4gICAgICAgICAgICBBdHRyaWJ1dGVOYW1lOiAndXNlcklkJyxcbiAgICAgICAgICAgIEtleVR5cGU6ICdIQVNIJyxcbiAgICAgICAgICB9LFxuICAgICAgICBdLFxuICAgICAgICBCaWxsaW5nTW9kZTogJ1BBWV9QRVJfUkVRVUVTVCcsXG4gICAgICAgIFBvaW50SW5UaW1lUmVjb3ZlcnlTcGVjaWZpY2F0aW9uOiB7XG4gICAgICAgICAgUG9pbnRJblRpbWVSZWNvdmVyeUVuYWJsZWQ6IHRydWUsXG4gICAgICAgIH0sXG4gICAgICB9KTtcbiAgICB9KTtcblxuICAgIHRlc3QoJ2NyZWF0ZXMgZ3JvdXBzIHRhYmxlIHdpdGggYWN0aXZlIGdyb3VwcyBpbmRleCcsICgpID0+IHtcbiAgICAgIHRlbXBsYXRlLmhhc1Jlc291cmNlUHJvcGVydGllcygnQVdTOjpEeW5hbW9EQjo6VGFibGUnLCB7XG4gICAgICAgIFRhYmxlTmFtZTogJ29yZ2FuaXplLWdyb3VwcycsXG4gICAgICAgIEdsb2JhbFNlY29uZGFyeUluZGV4ZXM6IE1hdGNoLmFycmF5V2l0aChbXG4gICAgICAgICAgTWF0Y2gub2JqZWN0TGlrZSh7XG4gICAgICAgICAgICBJbmRleE5hbWU6ICdhY3RpdmVHcm91cHNJbmRleCcsXG4gICAgICAgICAgICBLZXlTY2hlbWE6IFtcbiAgICAgICAgICAgICAgeyBBdHRyaWJ1dGVOYW1lOiAnYWN0aXZlJywgS2V5VHlwZTogJ0hBU0gnIH0sXG4gICAgICAgICAgICAgIHsgQXR0cmlidXRlTmFtZTogJ2NyZWF0ZWRBdCcsIEtleVR5cGU6ICdSQU5HRScgfSxcbiAgICAgICAgICAgIF0sXG4gICAgICAgICAgfSksXG4gICAgICAgIF0pLFxuICAgICAgfSk7XG4gICAgfSk7XG5cbiAgICB0ZXN0KCdjcmVhdGVzIGdyb3VwIG1lbWJlcnMgdGFibGUgd2l0aCB1c2VyIGdyb3VwcyBpbmRleCcsICgpID0+IHtcbiAgICAgIHRlbXBsYXRlLmhhc1Jlc291cmNlUHJvcGVydGllcygnQVdTOjpEeW5hbW9EQjo6VGFibGUnLCB7XG4gICAgICAgIFRhYmxlTmFtZTogJ29yZ2FuaXplLWdyb3VwLW1lbWJlcnMnLFxuICAgICAgICBHbG9iYWxTZWNvbmRhcnlJbmRleGVzOiBNYXRjaC5hcnJheVdpdGgoW1xuICAgICAgICAgIE1hdGNoLm9iamVjdExpa2Uoe1xuICAgICAgICAgICAgSW5kZXhOYW1lOiAndXNlckdyb3Vwc0luZGV4JyxcbiAgICAgICAgICB9KSxcbiAgICAgICAgXSksXG4gICAgICB9KTtcbiAgICB9KTtcblxuICAgIHRlc3QoJ2NyZWF0ZXMgZXZlbnRzIHRhYmxlIHdpdGggbXVsdGlwbGUgaW5kaWNlcycsICgpID0+IHtcbiAgICAgIHRlbXBsYXRlLmhhc1Jlc291cmNlUHJvcGVydGllcygnQVdTOjpEeW5hbW9EQjo6VGFibGUnLCB7XG4gICAgICAgIFRhYmxlTmFtZTogJ29yZ2FuaXplLWV2ZW50cycsXG4gICAgICAgIEdsb2JhbFNlY29uZGFyeUluZGV4ZXM6IE1hdGNoLmFycmF5V2l0aChbXG4gICAgICAgICAgTWF0Y2gub2JqZWN0TGlrZSh7IEluZGV4TmFtZTogJ2dyb3VwRXZlbnRzSW5kZXgnIH0pLFxuICAgICAgICAgIE1hdGNoLm9iamVjdExpa2UoeyBJbmRleE5hbWU6ICdkYXRlRXZlbnRzSW5kZXgnIH0pLFxuICAgICAgICBdKSxcbiAgICAgIH0pO1xuICAgIH0pO1xuXG4gICAgdGVzdCgnY3JlYXRlcyByc3ZwcyB0YWJsZSB3aXRoIHVzZXIgUlNWUHMgaW5kZXgnLCAoKSA9PiB7XG4gICAgICB0ZW1wbGF0ZS5oYXNSZXNvdXJjZVByb3BlcnRpZXMoJ0FXUzo6RHluYW1vREI6OlRhYmxlJywge1xuICAgICAgICBUYWJsZU5hbWU6ICdvcmdhbml6ZS1yc3ZwcycsXG4gICAgICAgIEdsb2JhbFNlY29uZGFyeUluZGV4ZXM6IE1hdGNoLmFycmF5V2l0aChbXG4gICAgICAgICAgTWF0Y2gub2JqZWN0TGlrZSh7IEluZGV4TmFtZTogJ3VzZXJSU1ZQc0luZGV4JyB9KSxcbiAgICAgICAgXSksXG4gICAgICB9KTtcbiAgICB9KTtcblxuICAgIHRlc3QoJ2NyZWF0ZXMgbWVzc2FnZXMgdGFibGUnLCAoKSA9PiB7XG4gICAgICB0ZW1wbGF0ZS5oYXNSZXNvdXJjZVByb3BlcnRpZXMoJ0FXUzo6RHluYW1vREI6OlRhYmxlJywge1xuICAgICAgICBUYWJsZU5hbWU6ICdvcmdhbml6ZS1tZXNzYWdlcycsXG4gICAgICAgIEtleVNjaGVtYTogW1xuICAgICAgICAgIHsgQXR0cmlidXRlTmFtZTogJ2dyb3VwSWQnLCBLZXlUeXBlOiAnSEFTSCcgfSxcbiAgICAgICAgICB7IEF0dHJpYnV0ZU5hbWU6ICd0aW1lc3RhbXAnLCBLZXlUeXBlOiAnUkFOR0UnIH0sXG4gICAgICAgIF0sXG4gICAgICB9KTtcbiAgICB9KTtcbiAgfSk7XG5cbiAgZGVzY3JpYmUoJ1MzIGFuZCBDbG91ZEZyb250JywgKCkgPT4ge1xuICAgIHRlc3QoJ2NyZWF0ZXMgUzMgYnVja2V0IHdpdGggd2Vic2l0ZSBjb25maWd1cmF0aW9uJywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OlMzOjpCdWNrZXQnLCB7XG4gICAgICAgIFdlYnNpdGVDb25maWd1cmF0aW9uOiB7XG4gICAgICAgICAgSW5kZXhEb2N1bWVudDogJ2luZGV4Lmh0bWwnLFxuICAgICAgICAgIEVycm9yRG9jdW1lbnQ6ICdpbmRleC5odG1sJyxcbiAgICAgICAgfSxcbiAgICAgICAgUHVibGljQWNjZXNzQmxvY2tDb25maWd1cmF0aW9uOiB7XG4gICAgICAgICAgQmxvY2tQdWJsaWNBY2xzOiB0cnVlLFxuICAgICAgICAgIEJsb2NrUHVibGljUG9saWN5OiB0cnVlLFxuICAgICAgICAgIElnbm9yZVB1YmxpY0FjbHM6IHRydWUsXG4gICAgICAgICAgUmVzdHJpY3RQdWJsaWNCdWNrZXRzOiB0cnVlLFxuICAgICAgICB9LFxuICAgICAgICBDb3JzQ29uZmlndXJhdGlvbjoge1xuICAgICAgICAgIENvcnNSdWxlczogW1xuICAgICAgICAgICAge1xuICAgICAgICAgICAgICBBbGxvd2VkTWV0aG9kczogWydHRVQnXSxcbiAgICAgICAgICAgICAgQWxsb3dlZE9yaWdpbnM6IFsnKiddLFxuICAgICAgICAgICAgICBBbGxvd2VkSGVhZGVyczogWycqJ10sXG4gICAgICAgICAgICB9LFxuICAgICAgICAgIF0sXG4gICAgICAgIH0sXG4gICAgICB9KTtcbiAgICB9KTtcblxuICAgIHRlc3QoJ2NyZWF0ZXMgQ2xvdWRGcm9udCBkaXN0cmlidXRpb24gd2l0aCBjdXN0b20gZG9tYWluJywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OkNsb3VkRnJvbnQ6OkRpc3RyaWJ1dGlvbicsIHtcbiAgICAgICAgRGlzdHJpYnV0aW9uQ29uZmlnOiBNYXRjaC5vYmplY3RMaWtlKHtcbiAgICAgICAgICBBbGlhc2VzOiBbJ29yZ2FuaXplLmRjdGVjaC5ldmVudHMnXSxcbiAgICAgICAgICBEZWZhdWx0Um9vdE9iamVjdDogJ2luZGV4Lmh0bWwnLFxuICAgICAgICAgIEN1c3RvbUVycm9yUmVzcG9uc2VzOiBNYXRjaC5hcnJheVdpdGgoW1xuICAgICAgICAgICAge1xuICAgICAgICAgICAgICBFcnJvckNvZGU6IDQwNCxcbiAgICAgICAgICAgICAgUmVzcG9uc2VDb2RlOiAyMDAsXG4gICAgICAgICAgICAgIFJlc3BvbnNlUGFnZVBhdGg6ICcvaW5kZXguaHRtbCcsXG4gICAgICAgICAgICB9LFxuICAgICAgICAgICAge1xuICAgICAgICAgICAgICBFcnJvckNvZGU6IDQwMyxcbiAgICAgICAgICAgICAgUmVzcG9uc2VDb2RlOiAyMDAsXG4gICAgICAgICAgICAgIFJlc3BvbnNlUGFnZVBhdGg6ICcvaW5kZXguaHRtbCcsXG4gICAgICAgICAgICB9LFxuICAgICAgICAgIF0pLFxuICAgICAgICB9KSxcbiAgICAgIH0pO1xuICAgIH0pO1xuXG4gICAgdGVzdCgnY3JlYXRlcyBvcmlnaW4gYWNjZXNzIGlkZW50aXR5IGZvciBDbG91ZEZyb250JywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUucmVzb3VyY2VDb3VudElzKCdBV1M6OkNsb3VkRnJvbnQ6OkNsb3VkRnJvbnRPcmlnaW5BY2Nlc3NJZGVudGl0eScsIDEpO1xuICAgIH0pO1xuICB9KTtcblxuICBkZXNjcmliZSgnUm91dGUgNTMgRE5TJywgKCkgPT4ge1xuICAgIHRlc3QoJ2NyZWF0ZXMgQSByZWNvcmQgZm9yIG9yZ2FuaXplIHN1YmRvbWFpbicsICgpID0+IHtcbiAgICAgIHRlbXBsYXRlLmhhc1Jlc291cmNlUHJvcGVydGllcygnQVdTOjpSb3V0ZTUzOjpSZWNvcmRTZXQnLCB7XG4gICAgICAgIE5hbWU6ICdvcmdhbml6ZS5kY3RlY2guZXZlbnRzLicsXG4gICAgICAgIFR5cGU6ICdBJyxcbiAgICAgIH0pO1xuICAgIH0pO1xuXG4gICAgdGVzdCgnY3JlYXRlcyBBQUFBIHJlY29yZCBmb3IgSVB2NiBzdXBwb3J0JywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OlJvdXRlNTM6OlJlY29yZFNldCcsIHtcbiAgICAgICAgTmFtZTogJ29yZ2FuaXplLmRjdGVjaC5ldmVudHMuJyxcbiAgICAgICAgVHlwZTogJ0FBQUEnLFxuICAgICAgfSk7XG4gICAgfSk7XG4gIH0pO1xuXG4gIGRlc2NyaWJlKCdMYW1iZGEgRnVuY3Rpb25zJywgKCkgPT4ge1xuICAgIHRlc3QoJ2NyZWF0ZXMgQVBJIExhbWJkYSBmdW5jdGlvbiB3aXRoIGNvcnJlY3QgcnVudGltZScsICgpID0+IHtcbiAgICAgIHRlbXBsYXRlLmhhc1Jlc291cmNlUHJvcGVydGllcygnQVdTOjpMYW1iZGE6OkZ1bmN0aW9uJywge1xuICAgICAgICBSdW50aW1lOiAnbm9kZWpzMjAueCcsXG4gICAgICAgIEhhbmRsZXI6ICdpbmRleC5oYW5kbGVyJyxcbiAgICAgICAgVGltZW91dDogMzAsXG4gICAgICAgIE1lbW9yeVNpemU6IDUxMixcbiAgICAgICAgRW52aXJvbm1lbnQ6IHtcbiAgICAgICAgICBWYXJpYWJsZXM6IE1hdGNoLm9iamVjdExpa2Uoe1xuICAgICAgICAgICAgVVNFUlNfVEFCTEU6IE1hdGNoLmFueVZhbHVlKCksXG4gICAgICAgICAgICBHUk9VUFNfVEFCTEU6IE1hdGNoLmFueVZhbHVlKCksXG4gICAgICAgICAgICBFVkVOVFNfVEFCTEU6IE1hdGNoLmFueVZhbHVlKCksXG4gICAgICAgICAgfSksXG4gICAgICAgIH0sXG4gICAgICB9KTtcbiAgICB9KTtcblxuICAgIHRlc3QoJ2NyZWF0ZXMgZXhwb3J0IExhbWJkYSBmdW5jdGlvbiB3aXRoIGxvbmdlciB0aW1lb3V0JywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OkxhbWJkYTo6RnVuY3Rpb24nLCB7XG4gICAgICAgIFJ1bnRpbWU6ICdub2RlanMyMC54JyxcbiAgICAgICAgVGltZW91dDogMzAwLFxuICAgICAgICBNZW1vcnlTaXplOiAxMDI0LFxuICAgICAgfSk7XG4gICAgfSk7XG5cbiAgICB0ZXN0KCdncmFudHMgRHluYW1vREIgcmVhZC93cml0ZSBwZXJtaXNzaW9ucyB0byBBUEkgTGFtYmRhJywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OklBTTo6UG9saWN5Jywge1xuICAgICAgICBQb2xpY3lEb2N1bWVudDoge1xuICAgICAgICAgIFN0YXRlbWVudDogTWF0Y2guYXJyYXlXaXRoKFtcbiAgICAgICAgICAgIE1hdGNoLm9iamVjdExpa2Uoe1xuICAgICAgICAgICAgICBBY3Rpb246IE1hdGNoLmFycmF5V2l0aChbXG4gICAgICAgICAgICAgICAgJ2R5bmFtb2RiOkJhdGNoR2V0SXRlbScsXG4gICAgICAgICAgICAgICAgJ2R5bmFtb2RiOkdldEl0ZW0nLFxuICAgICAgICAgICAgICAgICdkeW5hbW9kYjpTY2FuJyxcbiAgICAgICAgICAgICAgICAnZHluYW1vZGI6UHV0SXRlbScsXG4gICAgICAgICAgICAgICAgJ2R5bmFtb2RiOlVwZGF0ZUl0ZW0nLFxuICAgICAgICAgICAgICBdKSxcbiAgICAgICAgICAgIH0pLFxuICAgICAgICAgIF0pLFxuICAgICAgICB9LFxuICAgICAgfSk7XG4gICAgfSk7XG5cbiAgICB0ZXN0KCdncmFudHMgQ29nbml0byBwZXJtaXNzaW9ucyB0byBBUEkgTGFtYmRhJywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OklBTTo6UG9saWN5Jywge1xuICAgICAgICBQb2xpY3lEb2N1bWVudDoge1xuICAgICAgICAgIFN0YXRlbWVudDogTWF0Y2guYXJyYXlXaXRoKFtcbiAgICAgICAgICAgIE1hdGNoLm9iamVjdExpa2Uoe1xuICAgICAgICAgICAgICBBY3Rpb246IFsnY29nbml0by1pZHA6R2V0VXNlcicsICdjb2duaXRvLWlkcDpBZG1pbkdldFVzZXInXSxcbiAgICAgICAgICAgIH0pLFxuICAgICAgICAgIF0pLFxuICAgICAgICB9LFxuICAgICAgfSk7XG4gICAgfSk7XG4gIH0pO1xuXG4gIGRlc2NyaWJlKCdBUEkgR2F0ZXdheScsICgpID0+IHtcbiAgICB0ZXN0KCdjcmVhdGVzIFJFU1QgQVBJIHdpdGggQ09SUyBjb25maWd1cmF0aW9uJywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OkFwaUdhdGV3YXk6OlJlc3RBcGknLCB7XG4gICAgICAgIE5hbWU6ICdPcmdhbml6ZSBEQyBUZWNoIEV2ZW50cyBBUEknLFxuICAgICAgfSk7XG4gICAgfSk7XG5cbiAgICB0ZXN0KCdjcmVhdGVzIHByb3h5IHJlc291cmNlIGZvciBMYW1iZGEgaW50ZWdyYXRpb24nLCAoKSA9PiB7XG4gICAgICB0ZW1wbGF0ZS5oYXNSZXNvdXJjZVByb3BlcnRpZXMoJ0FXUzo6QXBpR2F0ZXdheTo6UmVzb3VyY2UnLCB7XG4gICAgICAgIFBhdGhQYXJ0OiAne3Byb3h5K30nLFxuICAgICAgfSk7XG4gICAgfSk7XG5cbiAgICB0ZXN0KCdjcmVhdGVzIEFOWSBtZXRob2QgZm9yIHByb3h5IHJlc291cmNlJywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUuaGFzUmVzb3VyY2VQcm9wZXJ0aWVzKCdBV1M6OkFwaUdhdGV3YXk6Ok1ldGhvZCcsIHtcbiAgICAgICAgSHR0cE1ldGhvZDogJ0FOWScsXG4gICAgICAgIEF1dGhvcml6YXRpb25UeXBlOiAnTk9ORScsXG4gICAgICB9KTtcbiAgICB9KTtcbiAgfSk7XG5cbiAgZGVzY3JpYmUoJ0V2ZW50QnJpZGdlJywgKCkgPT4ge1xuICAgIHRlc3QoJ2NyZWF0ZXMgc2NoZWR1bGVkIHJ1bGUgZm9yIGV4cG9ydCBMYW1iZGEnLCAoKSA9PiB7XG4gICAgICB0ZW1wbGF0ZS5oYXNSZXNvdXJjZVByb3BlcnRpZXMoJ0FXUzo6RXZlbnRzOjpSdWxlJywge1xuICAgICAgICBTY2hlZHVsZUV4cHJlc3Npb246ICdyYXRlKDUgbWludXRlcyknLFxuICAgICAgfSk7XG4gICAgfSk7XG5cbiAgICB0ZXN0KCdjb25maWd1cmVzIGV4cG9ydCBMYW1iZGEgYXMgcnVsZSB0YXJnZXQnLCAoKSA9PiB7XG4gICAgICB0ZW1wbGF0ZS5oYXNSZXNvdXJjZVByb3BlcnRpZXMoJ0FXUzo6RXZlbnRzOjpSdWxlJywge1xuICAgICAgICBUYXJnZXRzOiBNYXRjaC5hcnJheVdpdGgoW1xuICAgICAgICAgIE1hdGNoLm9iamVjdExpa2Uoe1xuICAgICAgICAgICAgQXJuOiBNYXRjaC5hbnlWYWx1ZSgpLFxuICAgICAgICAgIH0pLFxuICAgICAgICBdKSxcbiAgICAgIH0pO1xuICAgIH0pO1xuICB9KTtcblxuICBkZXNjcmliZSgnU3RhY2sgT3V0cHV0cycsICgpID0+IHtcbiAgICB0ZXN0KCdleHBvcnRzIGFsbCByZXF1aXJlZCBvdXRwdXRzJywgKCkgPT4ge1xuICAgICAgY29uc3Qgb3V0cHV0cyA9IE9iamVjdC5rZXlzKHRlbXBsYXRlLmZpbmRPdXRwdXRzKCcqJykpO1xuICAgICAgXG4gICAgICBleHBlY3Qob3V0cHV0cykudG9Db250YWluKCdVc2VyUG9vbElkJyk7XG4gICAgICBleHBlY3Qob3V0cHV0cykudG9Db250YWluKCdVc2VyUG9vbENsaWVudElkJyk7XG4gICAgICBleHBlY3Qob3V0cHV0cykudG9Db250YWluKCdBcGlVcmwnKTtcbiAgICAgIGV4cGVjdChvdXRwdXRzKS50b0NvbnRhaW4oJ0Nsb3VkRnJvbnRVcmwnKTtcbiAgICAgIGV4cGVjdChvdXRwdXRzKS50b0NvbnRhaW4oJ1dlYnNpdGVCdWNrZXROYW1lJyk7XG4gICAgfSk7XG4gIH0pO1xuXG4gIGRlc2NyaWJlKCdSZXNvdXJjZSBDb3VudCcsICgpID0+IHtcbiAgICB0ZXN0KCdjcmVhdGVzIGV4cGVjdGVkIG51bWJlciBvZiBEeW5hbW9EQiB0YWJsZXMnLCAoKSA9PiB7XG4gICAgICB0ZW1wbGF0ZS5yZXNvdXJjZUNvdW50SXMoJ0FXUzo6RHluYW1vREI6OlRhYmxlJywgNik7XG4gICAgfSk7XG5cbiAgICB0ZXN0KCdjcmVhdGVzIGV4cGVjdGVkIG51bWJlciBvZiBMYW1iZGEgZnVuY3Rpb25zJywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUucmVzb3VyY2VDb3VudElzKCdBV1M6OkxhbWJkYTo6RnVuY3Rpb24nLCAyKTtcbiAgICB9KTtcblxuICAgIHRlc3QoJ2NyZWF0ZXMgc2luZ2xlIEFQSSBHYXRld2F5JywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUucmVzb3VyY2VDb3VudElzKCdBV1M6OkFwaUdhdGV3YXk6OlJlc3RBcGknLCAxKTtcbiAgICB9KTtcblxuICAgIHRlc3QoJ2NyZWF0ZXMgc2luZ2xlIENsb3VkRnJvbnQgZGlzdHJpYnV0aW9uJywgKCkgPT4ge1xuICAgICAgdGVtcGxhdGUucmVzb3VyY2VDb3VudElzKCdBV1M6OkNsb3VkRnJvbnQ6OkRpc3RyaWJ1dGlvbicsIDEpO1xuICAgIH0pO1xuICB9KTtcbn0pO1xuIl19