#!/bin/bash
# Deploy with OAuth providers enabled
# Usage: ./deploy-with-oauth.sh

echo "🔐 Deploying with OAuth providers..."
echo ""

# Check if credentials are in environment variables (recommended)
if [ -n "$GOOGLE_CLIENT_ID" ] && [ -n "$GOOGLE_CLIENT_SECRET" ]; then
    echo "✅ Google credentials found in environment"
    GOOGLE_ARGS="--context googleClientId=$GOOGLE_CLIENT_ID --context googleClientSecret=$GOOGLE_CLIENT_SECRET"
else
    echo "⏭️  Google credentials not found (set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)"
    GOOGLE_ARGS=""
fi

if [ -n "$GITHUB_CLIENT_ID" ] && [ -n "$GITHUB_CLIENT_SECRET" ]; then
    echo "✅ GitHub credentials found in environment"
    GITHUB_ARGS="--context githubClientId=$GITHUB_CLIENT_ID --context githubClientSecret=$GITHUB_CLIENT_SECRET"
else
    echo "⏭️  GitHub credentials not found (set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET)"
    GITHUB_ARGS=""
fi

echo ""
echo "Deploying..."
npx cdk deploy $GOOGLE_ARGS $GITHUB_ARGS --require-approval never
