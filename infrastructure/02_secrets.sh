#!/bin/bash
# Phase 2 — Generate ECDSA signing key and store in Secrets Manager
set -euo pipefail

REGION="ap-south-1"
SECRET_NAME="digilux-ota-signing-key"

echo "==> Checking for existing OTA signing key"
if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" 2>/dev/null; then
  echo "    Secret already exists. To rotate the key, delete it first."
  echo "    Skipping key generation."
  exit 0
fi

echo "==> Generating ECDSA P-256 key pair"
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

openssl ecparam -name prime256v1 -genkey -noout -out "$TMPDIR/private.pem"
openssl ec -in "$TMPDIR/private.pem" -pubout -out "$TMPDIR/public.pem"

PRIVATE_KEY=$(cat "$TMPDIR/private.pem")
PUBLIC_KEY=$(cat "$TMPDIR/public.pem")

echo "==> Storing key pair in Secrets Manager: $SECRET_NAME"
aws secretsmanager create-secret \
  --name "$SECRET_NAME" \
  --region "$REGION" \
  --description "ECDSA P-256 key pair for Digilux OTA artifact signing" \
  --secret-string "{
    \"privateKey\": $(echo "$PRIVATE_KEY" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),
    \"publicKey\": $(echo "$PUBLIC_KEY" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),
    \"algorithm\": \"EC_PRIME256V1\",
    \"createdAt\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
  }"

echo ""
echo "OTA signing key stored in Secrets Manager: $SECRET_NAME"
echo ""
echo "Public key (distribute to controllers via shadow or provisioning):"
echo "---"
cat "$TMPDIR/public.pem"
echo "---"
echo ""
echo "IMPORTANT: Save the public key above. It must be deployed to all controllers."
echo "           It will be delivered to controllers via IoT Device Shadow named 'ota-config'."
