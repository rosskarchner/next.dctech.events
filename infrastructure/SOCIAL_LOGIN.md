# Social Login Setup

Social login (Google, GitHub, etc.) is **ready to enable** - add providers manually!

## Current Status

✅ OAuth callback handler implemented (`/callback` route)  
✅ Cognito User Pool configured  
✅ Supports manual provider management (no CDK redeployment needed)  
⏳ Waiting for OAuth providers to be added

## Why Manual?

Social login providers are managed **manually** (via AWS Console or CLI) rather than in CDK because:
- No need to store secrets in code or context
- Update OAuth credentials anytime without redeploying
- CDK won't delete manually-created providers
- More secure - credentials never in git or CloudFormation

## Quick Setup

### Option 1: AWS Console (Easiest)

1. **Get Google OAuth credentials**:
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create OAuth 2.0 Client ID
   - **Authorized redirect URI**: `https://organize-dctech-797438674243.auth.us-east-1.amazoncognito.com/oauth2/idpresponse`
   - Copy Client ID and Client Secret

2. **Add to Cognito**:
   - Go to [AWS Cognito Console](https://console.aws.amazon.com/cognito)
   - Select user pool: `organize-dctech-events-users`
   - Click **Sign-in experience** → **Federated identity provider sign-in**
   - Click **Add identity provider** → **Google**
   - Paste Client ID and Client Secret
   - Scopes: `profile email openid`
   - Map attributes:
     - Email → email
     - Name → name
     - Picture → picture
   - Click **Add identity provider**

3. **Done!** Users will now see "Continue with Google" button at login.

### Option 2: AWS CLI

```bash
# Add Google provider
aws cognito-idp create-identity-provider \
  --user-pool-id us-east-1_xYwCAsEJX \
  --provider-name Google \
  --provider-type Google \
  --provider-details '{
    "client_id":"YOUR_GOOGLE_CLIENT_ID",
    "client_secret":"YOUR_GOOGLE_CLIENT_SECRET",
    "authorize_scopes":"profile email openid"
  }' \
  --attribute-mapping '{
    "email":"email",
    "name":"name",
    "picture":"picture"
  }'
```

### GitHub Login

1. **Create GitHub OAuth App**:
   - Go to GitHub Settings → Developer settings → OAuth Apps → New OAuth App
   - Application name: `DC Tech Events`
   - Homepage URL: `https://next.dctech.events`
   - Authorization callback URL: `https://organize-dctech-797438674243.auth.us-east-1.amazoncognito.com/oauth2/idpresponse`
   - Copy Client ID and Client Secret

2. **Add via AWS CLI**:
   ```bash
   aws cognito-idp create-identity-provider \
     --user-pool-id us-east-1_xYwCAsEJX \
     --provider-name GitHub \
     --provider-type OIDC \
     --provider-details '{
       "client_id":"YOUR_GITHUB_CLIENT_ID",
       "client_secret":"YOUR_GITHUB_CLIENT_SECRET",
       "authorize_scopes":"read:user user:email",
       "oidc_issuer":"https://github.com/login/oauth",
       "attributes_request_method":"GET"
     }' \
     --attribute-mapping '{
       "email":"email",
       "name":"name",
       "picture":"avatar_url"
     }'
   ```

Note: GitHub requires OIDC provider type, not available in Console UI yet.

## Testing

After deploying:

1. Go to https://next.dctech.events/submit/
2. You'll be redirected to Cognito login
3. Click "Continue with Google" or "Continue with GitHub"
4. After login, you'll be redirected back to submit form

## Disable Password Login (Optional)

To use **ONLY** social login (no passwords), edit `lib/infrastructure-stack.ts`:

```typescript
const userPoolClient = new cognito.UserPoolClient(this, 'OrganizeUserPoolClient', {
  userPool,
  authFlows: {
    userPassword: false,  // Disable
    userSrp: false,       // Disable
  },
  supportedIdentityProviders: [
    // Remove COGNITO from list:
    cognito.UserPoolClientIdentityProvider.GOOGLE,
    cognito.UserPoolClientIdentityProvider.custom('GitHub'),
  ],
  // ...
});
```

## Cost

- Cognito: First 50,000 monthly active users (MAU) FREE
- Secrets Manager: $0.40/secret/month (~$1-2/month total)

## Troubleshooting

**"Invalid redirect URI"**  
→ Check OAuth app settings match: `https://organize-dctech-797438674243.auth.us-east-1.amazoncognito.com/oauth2/idpresponse`

**"Provider not found"**  
→ Ensure secrets exist in Secrets Manager  
→ Verify provider code is uncommented  
→ Run `npx cdk deploy` again

**Still see username/password form**  
→ This is normal - Cognito shows all enabled auth methods  
→ Users can choose social login button instead
