#!/bin/bash
# Phase 2 — Generate ECDSA signing key and store in Secrets Manager
set -euo pipefail
export AWS_PAGER="" PAGER=cat

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"
SECRET_NAME="${SIGNING_SECRET:-${PREFIX}-ota-signing-key}"

echo "==> OTA signing key: $SECRET_NAME  (region: $REGION)"

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
  --description "ECDSA P-256 key pair for OTA artifact signing (prefix: ${PREFIX})" \
  --secret-string "{
    \"privateKey\": $(echo "$PRIVATE_KEY" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),
    \"publicKey\":  $(echo "$PUBLIC_KEY"  | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),
    \"algorithm\":  \"EC_PRIME256V1\",
    \"createdAt\":  \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
  }"

echo ""
echo "OTA signing key stored: $SECRET_NAME"
echo ""
echo "Public key (embed in each controller at /etc/digilux/ota-signing.pub):"
echo "---"
cat "$TMPDIR/public.pem"
echo "---"
echo ""
echo "IMPORTANT: Save the public key above and distribute it to all controllers."
