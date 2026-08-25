#!/bin/bash
# Phase 1 — S3 artifact bucket
set -euo pipefail

REGION="ap-south-1"
BUCKET="digilux-ota-artifacts"

echo "==> Creating S3 bucket: $BUCKET"
if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
  echo "    Bucket already exists, skipping creation."
else
  aws s3api create-bucket \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION"
  echo "    Bucket created."
fi

echo "==> Enabling versioning"
aws s3api put-bucket-versioning \
  --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

echo "==> Blocking all public access"
aws s3api put-public-access-block \
  --bucket "$BUCKET" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "==> Enabling server-side encryption (AES256)"
aws s3api put-bucket-encryption \
  --bucket "$BUCKET" \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
      "BucketKeyEnabled": true
    }]
  }'

echo "==> Setting bucket policy (HTTPS-only + CloudFront OAC read access)"
# CloudFront distribution E2P92N4YRVH40S uses OAC (not legacy OAI).
# With OAC, the bucket must explicitly allow s3:GetObject from the
# cloudfront.amazonaws.com service principal scoped to the distribution ARN.
# Without this statement, CloudFront returns 403 even for valid signed URLs.
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws s3api put-bucket-policy \
  --bucket "$BUCKET" \
  --policy "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {
        \"Sid\": \"DenyNonHTTPS\",
        \"Effect\": \"Deny\",
        \"Principal\": \"*\",
        \"Action\": \"s3:*\",
        \"Resource\": [
          \"arn:aws:s3:::$BUCKET\",
          \"arn:aws:s3:::$BUCKET/*\"
        ],
        \"Condition\": {
          \"Bool\": { \"aws:SecureTransport\": \"false\" }
        }
      },
      {
        \"Sid\": \"AllowCloudFrontOAC\",
        \"Effect\": \"Allow\",
        \"Principal\": { \"Service\": \"cloudfront.amazonaws.com\" },
        \"Action\": \"s3:GetObject\",
        \"Resource\": \"arn:aws:s3:::$BUCKET/*\",
        \"Condition\": {
          \"StringEquals\": {
            \"AWS:SourceArn\": \"arn:aws:cloudfront::${ACCOUNT_ID}:distribution/E2P92N4YRVH40S\"
          }
        }
      }
    ]
  }"

echo "==> Adding lifecycle rule (abort incomplete multipart uploads after 7 days)"
aws s3api put-bucket-lifecycle-configuration \
  --bucket "$BUCKET" \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "abort-incomplete-multipart",
      "Status": "Enabled",
      "Filter": {"Prefix": ""},
      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}
    }]
  }'

echo "==> Creating folder structure"
EMPTY_FILE=$(mktemp)
for PREFIX in firmware/ application/ drivers/ zigbee-devices/ config/ rules/; do
  aws s3api put-object --bucket "$BUCKET" --key "${PREFIX}.keep" --body "$EMPTY_FILE" --region "$REGION" > /dev/null
  echo "    Created: $PREFIX"
done
rm -f "$EMPTY_FILE"

echo ""
echo "S3 bucket $BUCKET is ready."
