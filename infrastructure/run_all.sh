#!/bin/bash
# Master deployment script — runs all phases in order.
# Run from: /Users/maheshmaney/maney/digilux/aws-cloud/ota/infrastructure/
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
STEPS=(
  "01_s3.sh"
  "02_secrets.sh"
  "03_iot_setup.sh"
  "04_dynamodb.sh"
  "05_iam_roles.sh"
  "07_deploy_lambdas.sh"
  "08_iot_rules.sh"
  "09_api_gateway.sh"
  "10_cloudwatch.sh"
)

echo "========================================"
echo " Digilux OTA Infrastructure Deployment"
echo "========================================"
echo ""

for STEP in "${STEPS[@]}"; do
  echo "------------------------------------------------------------"
  echo " Running: $STEP"
  echo "------------------------------------------------------------"
  chmod +x "$DIR/$STEP"
  bash "$DIR/$STEP"
  echo ""
done

echo "========================================"
echo " Deployment Complete"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Retrieve the OTA public key:"
echo "     aws secretsmanager get-secret-value --secret-id digilux-ota-signing-key \\"
echo "       --query SecretString --output text | python3 -c \"import sys,json; print(json.load(sys.stdin)['publicKey'])\""
echo ""
echo "  2. Copy the public key to each controller at: /etc/digilux/ota-signing.pub"
echo ""
echo "  3. Install the OTA agent on each controller:"
echo "     scp -r ota/controller/ digilux@<controller-ip>:/tmp/ota-agent"
echo "     ssh digilux@<controller-ip> 'sudo /tmp/ota-agent/install.sh'"
echo ""
echo "  4. Register a test package:"
echo "     # Upload artifact to S3 first, then:"
echo "     POST https://ds6nxf8ac5.execute-api.ap-south-1.amazonaws.com/smarthome/api/v1/ota/packages"
echo ""
echo "  5. Trigger a canary deployment:"
echo "     POST /api/v1/ota/deployments"
echo "     { \"packageName\": \"controller-app\", \"version\": \"2.0.0\","
echo "       \"targetType\": \"THING_GROUP\", \"targetId\": \"DGX-Canary\","
echo "       \"rolloutStage\": \"CANARY\" }"
