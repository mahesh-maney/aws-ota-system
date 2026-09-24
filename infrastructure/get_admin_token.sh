#!/bin/bash
# Generate /tmp/ota_admin_token.txt (IdToken) for the e2e test suite.
#
# Usage:
#   ./infrastructure/get_admin_token.sh
#   ./infrastructure/get_admin_token.sh mahesh.maney@gmail.com DigiluxAdmin@2026
#
# ⚠  IMPORTANT: The API Gateway Cognito authorizer on admin routes validates the
#    IdToken, NOT the AccessToken.  Using the AccessToken produces 401 on every
#    admin endpoint.  This script extracts and saves the IdToken.

set -euo pipefail

ADMIN_EMAIL="${1:-mahesh.maney@gmail.com}"
ADMIN_PASSWORD="${2:-DigiluxAdmin@2026}"
CLIENT_ID="2qmig1uh220ttntbl0gfvcde4f"   # admin pool client
REGION="ap-south-1"

echo "Fetching admin IdToken for ${ADMIN_EMAIL}..."

ID_TOKEN=$(python3.9 -W ignore -c "
import boto3, sys
cognito = boto3.client('cognito-idp', region_name='${REGION}')
try:
    resp = cognito.initiate_auth(
        AuthFlow='USER_PASSWORD_AUTH',
        ClientId='${CLIENT_ID}',
        AuthParameters={
            'USERNAME': '${ADMIN_EMAIL}',
            'PASSWORD': '${ADMIN_PASSWORD}',
        },
    )
    print(resp['AuthenticationResult']['IdToken'])
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
")

echo "\$ID_TOKEN" > /tmp/ota_admin_token.txt
echo "  Saved IdToken to /tmp/ota_admin_token.txt"
echo "  Token starts with: \${ID_TOKEN:0:40}..."
