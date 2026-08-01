"""Shared configuration for the next.dctech.events parallel stack.

All references to existing production resources are by literal ID/ARN —
never CloudFormation cross-stack import/export — so this app stays fully
decoupled from the TypeScript CDK app in dctech.events/infrastructure.
"""

ACCOUNT = "797438674243"
REGION = "us-east-1"  # CloudFront-attached ACM certs must live in us-east-1

# New parallel site
DOMAIN = "next.dctech.events"
BASE_URL = f"https://{DOMAIN}"

# Existing production resources (referenced read-only, by literal ID)
HOSTED_ZONE_ID = "Z078066931R85FQDWCM3P"
ZONE_NAME = "dctech.events"
USER_POOL_ID = "us-east-1_8Ay4dTt8j"  # dctech-events-users (shared login)
COGNITO_HOSTED_UI_DOMAIN = "login.dctech.events"

# New isolated resources
TABLE_NAME = "dctech-events-next"
PREFIX = "dctech-events-next"  # resource-name prefix for all new resources

# Newsletter. SES allows one contact list per account, so the production
# 'newsletters' list is shared (like the Cognito pool) — isolation comes
# from this stack's own topic, template, and configuration set.
NEWSLETTER_CONTACT_LIST = "newsletters"
NEWSLETTER_TOPIC = "dctech-next"
NEWSLETTER_FROM_EMAIL = "newsletter@dctech.events"
NEWSLETTER_ADMIN_EMAIL = "ross@karchner.com"

TAGS = {
    "project": "dctech-events-next",
    "environment": "next",
    "managedBy": "CDK",
}
