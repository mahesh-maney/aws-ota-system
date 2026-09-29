#!/bin/bash
# Phase 3 — IoT Thing Types, Thing Groups, Fleet Indexing
set -euo pipefail
export AWS_PAGER="" PAGER=cat

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"

# IoT Thing Group hierarchy (configurable via deploy.config)
ROOT_GROUP="${IOT_ROOT_GROUP:-DIGILUX}"
PRODUCTION_GROUP="${IOT_PRODUCTION_GROUP:-PRODUCTION}"
GATEWAYS_GROUP="${IOT_GATEWAYS_GROUP:-GATEWAYS}"
TOUCH_PANELS_GROUP="${IOT_TOUCH_PANELS_GROUP:-TOUCH-PANELS}"
GATEWAY_MODELS="${IOT_GATEWAY_MODELS:-DGW-100,DGW-200}"
TOUCH_PANEL_MODELS="${IOT_TOUCH_PANEL_MODELS:-TP-100,TP-200}"

create_group_if_missing() {
  local NAME="$1"
  local PARENT="$2"
  local DESC="$3"

  if aws iot describe-thing-group --thing-group-name "$NAME" --region "$REGION" 2>/dev/null | grep -q '"thingGroupName"'; then
    echo "    $NAME already exists."
    return
  fi

  if [[ -n "$PARENT" ]]; then
    aws iot create-thing-group \
      --thing-group-name "$NAME" \
      --parent-group-name "$PARENT" \
      --region "$REGION" \
      --thing-group-properties "thingGroupDescription=$DESC" > /dev/null
  else
    aws iot create-thing-group \
      --thing-group-name "$NAME" \
      --region "$REGION" \
      --thing-group-properties "thingGroupDescription=$DESC" > /dev/null
  fi
  echo "    Created: $NAME"
}

# ── Thing Types ───────────────────────────────────────────────────────────────
echo "==> IoT Thing Types"
for TYPE in "${PREFIX}-network-controller" "${PREFIX}-zigbee-device"; do
  if aws iot describe-thing-type --thing-type-name "$TYPE" --region "$REGION" 2>/dev/null | grep -q '"thingTypeName"'; then
    echo "    $TYPE already exists."
  else
    aws iot create-thing-type \
      --thing-type-name "$TYPE" \
      --region "$REGION" \
      --thing-type-properties "thingTypeDescription=OTA managed device (${PREFIX})" > /dev/null
    echo "    Created: $TYPE"
  fi
done

# ── Thing Group Hierarchy ─────────────────────────────────────────────────────
# ROOT_GROUP
# └── PRODUCTION_GROUP
#     ├── GATEWAYS_GROUP
#     │   ├── DGW-100  ...
#     └── TOUCH_PANELS_GROUP
#         ├── TP-100   ...
echo ""
echo "==> IoT Thing Groups"

create_group_if_missing "$ROOT_GROUP" "" "Root OTA device group"
create_group_if_missing "$PRODUCTION_GROUP" "$ROOT_GROUP" "Production OTA devices"
create_group_if_missing "$GATEWAYS_GROUP" "$PRODUCTION_GROUP" "Gateway devices"
create_group_if_missing "$TOUCH_PANELS_GROUP" "$PRODUCTION_GROUP" "Touch panel devices"

# Gateway model leaf groups
IFS=',' read -ra GW_MODELS <<< "$GATEWAY_MODELS"
for MODEL in "${GW_MODELS[@]}"; do
  MODEL="${MODEL// /}"  # trim spaces
  create_group_if_missing "$MODEL" "$GATEWAYS_GROUP" "Gateway model: $MODEL"
done

# Touch panel model leaf groups
IFS=',' read -ra TP_MODELS <<< "$TOUCH_PANEL_MODELS"
for MODEL in "${TP_MODELS[@]}"; do
  MODEL="${MODEL// /}"
  create_group_if_missing "$MODEL" "$TOUCH_PANELS_GROUP" "Touch panel model: $MODEL"
done

# ── Enable Fleet Indexing ─────────────────────────────────────────────────────
echo ""
echo "==> Fleet Indexing"
CURRENT=$(aws iot get-indexing-configuration --region "$REGION" \
  --query 'thingIndexingConfiguration.thingIndexingMode' --output text 2>/dev/null || echo "OFF")

if [[ "$CURRENT" == "OFF" ]]; then
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
        {"name": "shadow.name.ota-state.reported.model",    "type": "String"},
        {"name": "shadow.name.ota-state.reported.hwRevision","type": "String"}
      ]
    }'
  echo "    Fleet indexing enabled."
else
  echo "    Fleet indexing already active ($CURRENT), skipping."
fi

echo ""
echo "IoT setup complete."
echo "  Root group      : $ROOT_GROUP"
echo "  Production group: $PRODUCTION_GROUP"
echo "  Gateways group  : $GATEWAYS_GROUP  (models: $GATEWAY_MODELS)"
echo "  Touch panels    : $TOUCH_PANELS_GROUP  (models: $TOUCH_PANEL_MODELS)"
