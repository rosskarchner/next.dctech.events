#!/usr/bin/env bash
# Deploy the templated /edit/ submission UI to the next.dctech.events bucket.
# Substitutes {{API_BASE}} and {{COGNITO_CLIENT_ID}} from CloudFormation
# outputs, then syncs to s3://<site-bucket>/edit/ and invalidates CloudFront.
set -euo pipefail
cd "$(dirname "$0")/.."

API_BASE=$(aws cloudformation describe-stacks --stack-name NextApiStack \
  --query "Stacks[0].Outputs[?OutputKey=='NextApiEndpoint'].OutputValue" --output text)
API_BASE="${API_BASE%/}"
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name NextCognitoClientStack \
  --query "Stacks[0].Outputs[?OutputKey=='NextUserPoolClientId'].OutputValue" --output text)
BUCKET=$(aws cloudformation describe-stacks --stack-name NextHostingStack \
  --query "Stacks[0].Outputs[?OutputKey=='NextSiteBucketName'].OutputValue" --output text)
DIST_ID=$(aws cloudformation describe-stacks --stack-name NextHostingStack \
  --query "Stacks[0].Outputs[?OutputKey=='NextDistributionId'].OutputValue" --output text)

echo "API_BASE=$API_BASE CLIENT_ID=$CLIENT_ID BUCKET=$BUCKET"

STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT
cp -r frontends/edit/. "$STAGING/"

find "$STAGING" -type f \( -name '*.js' -o -name '*.html' \) -print0 |
  xargs -0 sed -i \
    -e "s|{{API_BASE}}|$API_BASE|g" \
    -e "s|{{COGNITO_CLIENT_ID}}|$CLIENT_ID|g"

# no-cache means "revalidate before reusing", not "never cache": the browser
# still stores the file and an ETag match returns a cheap 304. Without an
# explicit Cache-Control, S3 sends none, and browsers fall back to heuristic
# freshness (roughly 10% of the age since Last-Modified) — so a returning
# visitor kept running the previous deploy's JS for hours. Invalidating
# CloudFront does not help there; it never reaches the browser's cache.
aws s3 sync "$STAGING/" "s3://$BUCKET/edit/" --delete \
  --cache-control "no-cache, must-revalidate"
aws cloudfront create-invalidation --distribution-id "$DIST_ID" \
  --paths '/edit/*' >/dev/null

echo "Deployed edit UI to https://dctech.events/edit/"
