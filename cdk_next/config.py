"""Shared configuration for the dctech.events stack.

This app began as a parallel build at next.dctech.events and now serves
production. References to resources owned by the older TypeScript CDK app
are by literal ID/ARN — never CloudFormation cross-stack import/export.
"""

ACCOUNT = "797438674243"
REGION = "us-east-1"  # CloudFront-attached ACM certs must live in us-east-1

# Public site. www is served identically, matching the previous GitHub Pages
# setup; the old site stays reachable at old.dctech.events.
DOMAIN = "dctech.events"
WWW_DOMAIN = f"www.{DOMAIN}"
BASE_URL = f"https://{DOMAIN}"

# Existing production resources (referenced read-only, by literal ID)
HOSTED_ZONE_ID = "Z078066931R85FQDWCM3P"
ZONE_NAME = "dctech.events"
USER_POOL_ID = "us-east-1_8Ay4dTt8j"  # dctech-events-users (shared login)
COGNITO_HOSTED_UI_DOMAIN = "login.dctech.events"

# New isolated resources
TABLE_NAME = "dctech-events-next"
PREFIX = "dctech-events-next"  # resource-name prefix for all new resources

# Newsletter. Uses the long-standing SES contact list and topic so existing
# subscribers carry over; only the template and configuration set are ours.
NEWSLETTER_CONTACT_LIST = "newsletters"
NEWSLETTER_TOPIC = "dctech"
NEWSLETTER_FROM_EMAIL = "newsletter@dctech.events"
NEWSLETTER_ADMIN_EMAIL = "ross@karchner.com"

TAGS = {
    "project": "dctech-events-next",
    "environment": "next",
    "managedBy": "CDK",
}
