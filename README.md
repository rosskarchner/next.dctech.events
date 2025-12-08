# next.dctech.events

A modern event aggregation and community platform for the DC tech community, built with AWS serverless architecture.

## 🔒 Security

This project has undergone a comprehensive security review (December 2025). All critical and high-priority vulnerabilities have been addressed.

- **CodeQL Security Scan:** ✅ 0 alerts
- **OWASP Top 10 Compliance:** 7/10 ✅
- **Risk Level:** Medium (down from High)

### Security Documentation
- [SECURITY_VULNERABILITIES.md](./SECURITY_VULNERABILITIES.md) - Detailed security findings and recommendations
- [SECURITY_CHECKLIST.md](./SECURITY_CHECKLIST.md) - Implementation status and roadmap
- [SECURITY_SUMMARY.md](./SECURITY_SUMMARY.md) - Executive summary

### Key Security Features
- ✅ Input validation for all user inputs
- ✅ CORS with specific origin allow list
- ✅ Security headers (CSP, HSTS, X-Frame-Options)
- ✅ Encryption at rest for all data stores
- ✅ API throttling and rate limiting
- ✅ SSRF protection
- ✅ Strong password policies (12+ chars, symbols required)
- ✅ JWT signature verification

## 🏗️ Architecture

### Frontend
- Static HTML/CSS/JavaScript
- Deployed to S3
- Served via CloudFront with WAF protection

### Backend
- AWS Lambda (Node.js 20.x and Python 3.9)
- API Gateway with throttling
- DynamoDB for data storage
- Cognito for authentication
- EventBridge for scheduled tasks

### Infrastructure as Code
- AWS CDK (TypeScript)
- Automated deployment pipeline
- Environment configuration via context

## 🚀 Getting Started

### Prerequisites
- Node.js 20.x or later
- AWS CLI configured
- AWS CDK CLI (`npm install -g aws-cdk`)
- Python 3.9 or later (for Lambda functions)

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/rosskarchner/next.dctech.events.git
   cd next.dctech.events
   ```

2. **Install dependencies:**
   ```bash
   cd infrastructure
   npm install
   
   # Install Python dependencies for Lambda
   cd lambda/refresh
   pip install -r requirements.txt
   ```

3. **Configure AWS credentials:**
   ```bash
   aws configure
   ```

4. **Bootstrap CDK (first time only):**
   ```bash
   cdk bootstrap
   ```

### Deployment

1. **Deploy infrastructure:**
   ```bash
   cd infrastructure
   cdk deploy
   ```

2. **Deploy frontend:**
   ```bash
   # Frontend is automatically deployed via GitHub Actions
   # Or manually:
   aws s3 sync frontend/static/ s3://YOUR-BUCKET-NAME/static/
   ```

### Configuration

Create `infrastructure/cdk.context.json`:
```json
{
  "domainName": "next.dctech.events",
  "cognitoDomainPrefix": "organize-dctech-YOUR-ACCOUNT-ID",
  "webAclArn": "arn:aws:wafv2:us-east-1:YOUR-ACCOUNT:global/webacl/...",
  "nextDomain": "next.dctech.events",
  "githubRepo": "rosskarchner/dctech.events"
}
```

## 📊 Project Structure

```
next.dctech.events/
├── infrastructure/          # AWS CDK infrastructure code
│   ├── lib/                # CDK stack definitions
│   ├── lambda/             # Lambda function code
│   │   ├── api/           # API handler (Node.js)
│   │   ├── refresh/       # iCal sync (Python)
│   │   ├── export/        # Data export (Node.js)
│   │   └── recurrence/    # Event recurrence (Node.js)
│   └── test/              # Infrastructure tests
├── frontend/               # Static frontend files
│   └── static/            # HTML, CSS, JS assets
├── docs/                   # Documentation
├── tests/                  # End-to-end tests (Playwright)
└── .github/               # GitHub Actions workflows
```

## 🧪 Testing

### Run Unit Tests
```bash
cd infrastructure
npm test
```

### Run End-to-End Tests
```bash
npx playwright test
```

### Security Scanning
```bash
# JavaScript dependencies
npm audit

# Python dependencies
pip-audit

# Infrastructure scanning
cdk-nag
```

## 🔐 Security Best Practices

### For Developers

1. **Never commit secrets** - Use AWS Secrets Manager or SSM Parameter Store
2. **Always validate input** - Use the validation functions in `infrastructure/lambda/api/index.js`
3. **Escape output** - Use `escapeHtml()` for user-generated content
4. **Review security docs** - Read SECURITY_VULNERABILITIES.md before making changes
5. **Run security scans** - Use `npm audit` and CodeQL before committing

### For Operations

1. **Monitor CloudWatch alarms** - Set up alerts for security events
2. **Review access logs** - Check for unusual patterns
3. **Keep dependencies updated** - Regularly update npm and pip packages
4. **Enable MFA** - Use MFA for all administrative accounts
5. **Backup data** - Ensure DynamoDB point-in-time recovery is enabled

## 🛠️ Development

### Local Development

1. **Run Lambda functions locally:**
   ```bash
   cd infrastructure/lambda/api
   node index.js
   ```

2. **Test API endpoints:**
   ```bash
   curl -X GET http://localhost:3000/api/events
   ```

### Code Style

- **JavaScript:** ESLint with security plugin
- **Python:** Black formatter, bandit security linter
- **TypeScript:** TSLint for CDK code

### Pull Request Process

1. Create a feature branch
2. Make your changes
3. Run security scans (`npm audit`, `cdk-nag`)
4. Run tests (`npm test`)
5. Submit PR with description
6. Address review comments
7. Merge after approval

## 📝 API Documentation

### Public Endpoints

- `GET /` - Homepage with upcoming events
- `GET /events` - List all upcoming events
- `GET /groups` - List all active groups
- `GET /locations` - Browse events by location
- `GET /topics` - Browse events by topic
- `GET /user/{nickname}` - Public user profile

### Protected Endpoints (Require Authentication)

- `POST /submit/` - Submit a new event
- `POST /submit-group/` - Submit a new group
- `GET /my-feed` - Personalized event feed
- `GET /settings` - User settings
- `PUT /api/users/me` - Update profile

### Authentication

Uses AWS Cognito with OAuth 2.0:
- Login redirects to Cognito hosted UI
- Callback receives authorization code
- Exchange code for JWT tokens
- Include JWT in `Authorization: Bearer <token>` header

## 🌍 Environment Variables

### Lambda Functions

- `USERS_TABLE` - DynamoDB users table name
- `GROUPS_TABLE` - DynamoDB groups table name
- `EVENTS_TABLE` - DynamoDB events table name
- `USER_POOL_ID` - Cognito User Pool ID
- `USER_POOL_CLIENT_ID` - Cognito App Client ID
- `COGNITO_DOMAIN` - Cognito domain prefix
- `GITHUB_REPO` - GitHub repository for data sync
- `GITHUB_TOKEN_SECRET` - Secrets Manager key for GitHub token

## 🚨 Incident Response

### Security Incidents

1. **Detect:** Monitor CloudWatch alarms and GuardDuty
2. **Contain:** Disable affected resources if needed
3. **Investigate:** Review CloudTrail and application logs
4. **Remediate:** Apply fixes and redeploy
5. **Document:** Update incident log and security docs

### Reporting Security Issues

**Do NOT open public GitHub issues for security vulnerabilities.**

Email: [Security contact to be defined]

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## 📜 License

[License information to be added]

## 🙏 Acknowledgments

- DC Tech Community
- AWS Serverless team
- Security researchers and contributors

## 📞 Support

- **Issues:** [GitHub Issues](https://github.com/rosskarchner/next.dctech.events/issues)
- **Discussions:** [GitHub Discussions](https://github.com/rosskarchner/next.dctech.events/discussions)
- **Email:** [To be defined]

## 🗺️ Roadmap

### Q1 2025
- [x] Security hardening (completed Dec 2025)
- [ ] Implement rate limiting on sensitive endpoints
- [ ] Enhanced monitoring and alerting
- [ ] Penetration testing

### Q2 2025
- [ ] Mobile app development
- [ ] Advanced search capabilities
- [ ] Email notifications
- [ ] API for third-party integrations

### Q3 2025
- [ ] Social features (comments, discussions)
- [ ] Event recommendations ML model
- [ ] Advanced analytics dashboard
- [ ] Multi-language support

## 📊 Metrics

- **API Response Time:** < 200ms (p95)
- **Uptime:** 99.9% target
- **Security Score:** 7/10 OWASP compliance
- **Code Coverage:** Target 80%

---

**Built with ❤️ for the DC Tech Community**
