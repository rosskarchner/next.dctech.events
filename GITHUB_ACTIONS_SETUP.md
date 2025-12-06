# GitHub Actions Frontend Deployment Setup

## Overview

The frontend static assets are now deployed via GitHub Actions instead of a CDK custom resource. This provides faster deployments and separates infrastructure from application code.

## Architecture Changes

### Directory Structure
```
frontend/                    # Moved from infrastructure/frontend
├── public/                  # HTML files
│   ├── *.html
│   ├── css/
│   └── js/
└── static/                  # Static assets (deployed to S3)
    ├── css/
    └── js/

.github/
└── workflows/
    └── deploy-frontend.yml  # GitHub Actions workflow
```

### Infrastructure Changes

**Added:**
- IAM User: `github-actions-frontend-deploy`
- IAM Policy: `frontend-deploy-policy` with permissions for:
  - S3: Upload/delete objects in website bucket
  - CloudFront: Create invalidations

**Removed:**
- `DeployStaticFunction` Lambda
- Custom resource for deployment
- `lambda/deploy-static/` directory

## Setup Instructions

### 1. Deploy the Updated Infrastructure

```bash
cd infrastructure
cdk deploy
```

Note the outputs:
- `WebsiteBucketName`
- `CloudFrontDistributionId`
- `GitHubActionsUserName`

### 2. Create AWS Access Keys

```bash
# Create access keys for the GitHub Actions user
aws iam create-access-key --user-name github-actions-frontend-deploy
```

Save the `AccessKeyId` and `SecretAccessKey` from the output.

### 3. Configure GitHub Secrets

Add these secrets to your GitHub repository:

1. Go to: `Settings` → `Secrets and variables` → `Actions`
2. Add the following secrets:
   - `AWS_ACCESS_KEY_ID` - From step 2
   - `AWS_SECRET_ACCESS_KEY` - From step 2
   - `S3_BUCKET_NAME` - From CDK output `WebsiteBucketName`
   - `CLOUDFRONT_DISTRIBUTION_ID` - From CDK output `CloudFrontDistributionId`

## How It Works

### Automatic Deployments

The workflow triggers automatically when:
- Changes are pushed to `main` branch in the `frontend/` directory
- Manually triggered via GitHub Actions UI

### Deployment Process

1. Syncs `frontend/static/*` to `s3://bucket/static/`
2. Sets cache headers: `public, max-age=31536000, immutable` (1 year)
3. Deletes removed files from S3
4. Invalidates CloudFront cache for `/static/*`

### Manual Trigger

To manually deploy:
1. Go to `Actions` tab in GitHub
2. Select "Deploy Frontend to S3" workflow
3. Click "Run workflow"

## Development Workflow

### Making Frontend Changes

```bash
# Edit files in frontend/
vim frontend/static/css/main.css

# Commit and push
git add frontend/
git commit -m "Update styles"
git push origin main
```

The GitHub Action will automatically deploy to S3 and invalidate CloudFront.

### Testing Before Deploy

To test locally before pushing, you can use the AWS CLI:

```bash
# Preview what would be synced
aws s3 sync frontend/static/ s3://your-bucket/static/ --dryrun

# Manual sync (if needed)
aws s3 sync frontend/static/ s3://your-bucket/static/
```

## Security Notes

- The IAM user has minimal permissions (S3 upload + CloudFront invalidation only)
- Access keys should be rotated periodically
- Never commit AWS credentials to the repository
- GitHub Secrets are encrypted and only accessible to workflows

## Troubleshooting

### Check Workflow Logs
Go to `Actions` tab → Select workflow run → View logs

### Common Issues

**Workflow fails with S3 permissions error:**
- Verify the IAM user has the correct policy attached
- Check that the S3 bucket name in GitHub secrets matches the actual bucket

**CloudFront still showing old content:**
- Invalidations can take 1-2 minutes to complete
- Check that the distribution ID in secrets is correct
- Browser cache may also need to be cleared

**Workflow doesn't trigger:**
- Ensure changes are in the `frontend/` directory
- Check that you're pushing to the `main` branch
- Verify the workflow file is in `.github/workflows/`
