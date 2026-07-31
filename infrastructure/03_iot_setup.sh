#!/bin/bash
# Phase 3 — IoT Thing Types, Thing Groups, Fleet Indexing
set -euo pipefail

REGION="ap-south-1"

# ── Thing Types ──────────────────────────────────────────────────────────────
echo "==> Creating IoT Thing Types"

for TYPE in digilux-network-controller digilux-zigbee-device; do
  if aws iot describe-thing-type --thing-type-name "$TYPE" --region "$REGION" 2>/dev/null; then
    echo "    $TYPE already exists."
  else
    aws iot create-thing-type \
      --thing-type-name "$TYPE" \
      --region "$REGION" \
      --thing-type-properties "thingTypeDescription=Digilux $TYPE"
    echo "    Created: $TYPE"
  fi
done

# ── Thing Groups ──────────────────────────────────────────────────────────────
echo "==> Creating IoT Thing Groups"

# Parent group
if aws iot describe-thing-group --thing-group-name "DGX-Controllers" --region "$REGION" 2>/dev/null; then
  echo "    DGX-Controllers already exists."
else
  aws iot create-thing-group \
    --thing-group-name "DGX-Controllers" \
    --region "$REGION" \
    --thing-group-properties "thingGroupDescription=All Digilux network controllers"
  echo "    Created: DGX-Controllers"
fi

# Child groups under DGX-Controllers (bash 3-compatible: parallel arrays)
CHILD_GROUPS=("DGX-Canary" "DGX-Beta" "DGX-Production")
CHILD_DESCS=(
  "Canary fleet - 5 internal/test controllers"
  "Beta fleet - early adopter controllers (~10%)"
  "Production fleet - remaining controllers"
)

for i in 0 1 2; do
  GROUP="${CHILD_GROUPS[$i]}"
  DESC="${CHILD_DESCS[$i]}"
  if aws iot describe-thing-group --thing-group-name "$GROUP" --region "$REGION" 2>/dev/null; then
    echo "    $GROUP already exists."
  else
    aws iot create-thing-group \
      --thing-group-name "$GROUP" \
      --parent-group-name "DGX-Controllers" \
      --region "$REGION" \
      --thing-group-properties "thingGroupDescription=$DESC"
    echo "    Created: $GROUP"
  fi
done

# ── Enable Fleet Indexing (needed for querying things by shadow attributes) ──
echo "==> Enabling Fleet Indexing"
CURRENT=$(aws iot get-indexing-configuration --region "$REGION" \
  --query 'thingIndexingConfiguration.thingIndexingMode' --output text 2>/dev/null || echo "OFF")

if [ "$CURRENT" = "OFF" ]; then
  aws iot update-indexing-configuration \
    --region "$REGION" \
    --thing-indexing-configuration '{
      "thingIndexingMode": "REGISTRY_AND_SHADOW",
      "thingConnectivityIndexingMode": "STATUS",
      "namedShadowIndexingMode": "ON",
      "filter": {
        "namedShadowNames": ["ota-state"]
      },
      "managedFields": [],
      "customFields": [
        {"name": "shadow.name.ota-state.reported.deviceId", "type": "String"},
        {"name": "shadow.name.ota-state.reported.model", "type": "String"},
        {"name": "shadow.name.ota-state.reported.hwRevision", "type": "String"}
      ]
    }'
  echo "    Fleet indexing enabled with named shadow support."
else
  echo "    Fleet indexing already active ($CURRENT), skipping."
fi

echo ""
echo "IoT Thing Types and Groups are ready."
