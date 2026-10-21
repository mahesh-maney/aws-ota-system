#!/bin/bash
# Phase 3 — IoT Thing Types, Thing Groups, Fleet Indexing
set -euo pipefail
export AWS_PAGER="" PAGER=cat

REGION="${REGION:-ap-south-1}"
PREFIX="${PREFIX:-digilux}"

# IoT Thing Group hierarchy (configurable via deploy.config)
# Minimal 2-level example (Honeywell):
#   IOT_ROOT_GROUP=HON-PROD
#   IOT_PRODUCTION_GROUP=             # leave blank
#   IOT_GATEWAYS_GROUP=HON-ZigbeeGateway
#   IOT_TOUCH_PANELS_GROUP=           # leave blank
#   IOT_GATEWAY_MODELS=               # leave blank — no model-level groups
#   IOT_TOUCH_PANEL_MODELS=           # leave blank
#
# Full 4-level example (Digilux):
#   IOT_ROOT_GROUP=DIGILUX
#   IOT_PRODUCTION_GROUP=PRODUCTION
#   IOT_GATEWAYS_GROUP=GATEWAYS
#   IOT_TOUCH_PANELS_GROUP=TOUCH-PANELS
#   IOT_GATEWAY_MODELS=DGW-100,DGW-200
#   IOT_TOUCH_PANEL_MODELS=TP-100,TP-200
ROOT_GROUP="${IOT_ROOT_GROUP:-DIGILUX}"
PRODUCTION_GROUP="${IOT_PRODUCTION_GROUP:-}"
GATEWAYS_GROUP="${IOT_GATEWAYS_GROUP:-}"
TOUCH_PANELS_GROUP="${IOT_TOUCH_PANELS_GROUP:-}"
GATEWAY_MODELS="${IOT_GATEWAY_MODELS:-}"
TOUCH_PANEL_MODELS="${IOT_TOUCH_PANEL_MODELS:-}"

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
for TYPE in "ota-network-controller" "ota-zigbee-device"; do
  if aws iot describe-thing-type --thing-type-name "$TYPE" --region "$REGION" 2>/dev/null | grep -q '"thingTypeName"'; then
    echo "    $TYPE already exists."
  else
    aws iot create-thing-type \
      --thing-type-name "$TYPE" \
      --region "$REGION" \
      --thing-type-properties "thingTypeDescription=OTA managed device type" > /dev/null
    echo "    Created: $TYPE"
  fi
done

# ── Thing Group Hierarchy ─────────────────────────────────────────────────────
# Only groups with non-empty names are created. Empty vars are skipped entirely.
# Resulting hierarchy depends on deploy.config:
#
#   Minimal (2-level):        Full (4-level):
#   ROOT_GROUP                ROOT_GROUP
#   └── GATEWAYS_GROUP        └── PRODUCTION_GROUP
#                                 ├── GATEWAYS_GROUP
#                                 │   ├── model-1 ...
#                                 └── TOUCH_PANELS_GROUP
#                                     ├── model-1 ...
echo ""
echo "==> IoT Thing Groups"

# Level 1 — root (always created)
create_group_if_missing "$ROOT_GROUP" "" "Root OTA device group"

# Level 2 — production (optional)
if [[ -n "$PRODUCTION_GROUP" ]]; then
  create_group_if_missing "$PRODUCTION_GROUP" "$ROOT_GROUP" "Production OTA devices"
  _GATEWAYS_PARENT="$PRODUCTION_GROUP"
  _TOUCH_PARENT="$PRODUCTION_GROUP"
else
  # No production layer — gateways/touch-panels hang directly under root
  _GATEWAYS_PARENT="$ROOT_GROUP"
  _TOUCH_PARENT="$ROOT_GROUP"
fi

# Level 3 — gateways group (optional)
if [[ -n "$GATEWAYS_GROUP" ]]; then
  create_group_if_missing "$GATEWAYS_GROUP" "$_GATEWAYS_PARENT" "Gateway devices"
fi

# Level 3 — touch panels group (optional)
if [[ -n "$TOUCH_PANELS_GROUP" ]]; then
  create_group_if_missing "$TOUCH_PANELS_GROUP" "$_TOUCH_PARENT" "Touch panel devices"
fi

# Level 4 — gateway model leaf groups (optional)
if [[ -n "$GATEWAY_MODELS" && -n "$GATEWAYS_GROUP" ]]; then
  IFS=',' read -ra GW_MODELS <<< "$GATEWAY_MODELS"
  for MODEL in "${GW_MODELS[@]}"; do
    MODEL="${MODEL// /}"
    [[ -n "$MODEL" ]] && create_group_if_missing "$MODEL" "$GATEWAYS_GROUP" "Gateway model: $MODEL"
  done
fi

# Level 4 — touch panel model leaf groups (optional)
if [[ -n "$TOUCH_PANEL_MODELS" && -n "$TOUCH_PANELS_GROUP" ]]; then
  IFS=',' read -ra TP_MODELS <<< "$TOUCH_PANEL_MODELS"
  for MODEL in "${TP_MODELS[@]}"; do
    MODEL="${MODEL// /}"
    [[ -n "$MODEL" ]] && create_group_if_missing "$MODEL" "$TOUCH_PANELS_GROUP" "Touch panel model: $MODEL"
  done
fi

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
[[ -n "$PRODUCTION_GROUP" ]]  && echo "  Production group: $PRODUCTION_GROUP"
[[ -n "$GATEWAYS_GROUP" ]]    && echo "  Gateways group  : $GATEWAYS_GROUP${GATEWAY_MODELS:+  (models: $GATEWAY_MODELS)}"
[[ -n "$TOUCH_PANELS_GROUP" ]] && echo "  Touch panels    : $TOUCH_PANELS_GROUP${TOUCH_PANEL_MODELS:+  (models: $TOUCH_PANEL_MODELS)}"
