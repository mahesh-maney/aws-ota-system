"""
Test suite for digilux_ota_device_register

Coverage:
  Basic registration
    - First-time registration calls _assign_to_thing_group
    - Reconnect does NOT call _assign_to_thing_group
    - Missing deviceId → early return, no DB write
    - Device not in DB → early return, no group assignment

  Device model resolution
    - deviceModel in event → used for hierarchy placement
    - deviceModel not in event, in DB → falls back to DB value
    - deviceModel in both → event value wins
    - No deviceModel anywhere → fallback to PRODUCTION_GROUP directly

  Thing group hierarchy
    - Gateway model (DGW-100) → placed in DGW-100 group under GATEWAYS
    - Gateway model (DGW-200) → placed in DGW-200 group under GATEWAYS
    - Touch panel model (TP-100) → placed in TP-100 group under TOUCH-PANELS
    - Touch panel model (TP-200) → placed in TP-200 group under TOUCH-PANELS
    - Unknown model → placed directly in PRODUCTION_GROUP
    - Case insensitive model matching (dgw-100 matches DGW-100)

  Hierarchy creation (_ensure_hierarchy)
    - Creates ROOT → PRODUCTION → GATEWAYS/TOUCH-PANELS → leaf groups
    - ResourceAlreadyExistsException is silently ignored
    - Error in hierarchy creation does not block registration

  Fallback behaviour
    - Leaf group add fails → falls back to PRODUCTION_GROUP
    - Canary group full → device NOT added to canary
    - Canary group not full → device added to canary

  DynamoDB updates
    - deviceModel stored in DB when present
    - deviceModel NOT stored when absent (no extra field written)
    - globalInstalledVersion stored correctly

  Audit logs
    - First registration emits DEVICE_FIRST_REGISTRATION audit
    - Reconnect emits DEVICE_RECONNECTED audit

Run:
  pytest infrastructure/tests/test_device_register.py -v
"""

import json
import os
import sys
import importlib
import importlib.util
from unittest.mock import MagicMock, call, patch
from botocore.exceptions import ClientError

import pytest

# ── Path / env setup ──────────────────────────────────────────────────────────
os.environ.setdefault("REGION", "ap-south-1")
os.environ.setdefault("ACCOUNT_ID", "123456789")

_lf_path = os.path.join(
    os.path.dirname(__file__), "..", "06_lambdas",
    "digilux_ota_device_register", "lambda_function.py",
)
_spec = importlib.util.spec_from_file_location("lf_device_register", _lf_path)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)

# ── Helpers ───────────────────────────────────────────────────────────────────

DEVICE_ID = "edb39bba-baf1-4700-968c-a42228e53aa0"
MAC       = "aa:bb:cc:dd:ee:ff"
THING     = DEVICE_ID


def _event(device_id=DEVICE_ID, version="1.0.0", pkg_name="HomeAssistantUtility",
           device_model=None):
    e = {
        "deviceId":              device_id,
        "globalInstalledVersion": version,
        "package": {"name": pkg_name, "installedVersion": version},
    }
    if device_model is not None:
        e["deviceModel"] = device_model
    return e


def _existing(device_id=DEVICE_ID, has_thing=False, device_model=None):
    d = {
        "deviceId":   device_id,
        "macAddress": MAC,
        "installedVersion": "0.9.0",
    }
    if has_thing:
        d["thingName"] = device_id
    if device_model:
        d["deviceModel"] = device_model
    return d


def _make_dynamo_mock(existing_item=None):
    """Returns a mock dynamo resource whose Table().query() returns existing_item."""
    table_mock = MagicMock()
    table_mock.query.return_value = {
        "Items": [existing_item] if existing_item else []
    }
    table_mock.update_item.return_value = {}
    dynamo_mock = MagicMock()
    dynamo_mock.Table.return_value = table_mock
    return dynamo_mock, table_mock


def _make_iot_mock(canary_count=0):
    iot_mock = MagicMock()
    iot_mock.create_thing_group.return_value = {}
    iot_mock.add_thing_to_thing_group.return_value = {}
    iot_mock.list_things_in_thing_group.return_value = {
        "things": [f"thing-{i}" for i in range(canary_count)]
    }
    return iot_mock


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


# ── Basic registration tests ──────────────────────────────────────────────────

class TestBasicRegistration:
    def test_first_time_registration_assigns_group(self):
        dynamo_mock, table_mock = _make_dynamo_mock(_existing(has_thing=False))
        iot_mock = _make_iot_mock(canary_count=0)

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(), None)

        # update_item must be called
        assert table_mock.update_item.called

        # At least one add_thing_to_thing_group call
        assert iot_mock.add_thing_to_thing_group.called

    def test_reconnect_does_not_reassign_group(self):
        """Existing device (has thingName) must NOT trigger group assignment."""
        dynamo_mock, table_mock = _make_dynamo_mock(_existing(has_thing=True))
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(), None)

        # DB update happens (lastSeen etc.)
        assert table_mock.update_item.called
        # But NO group assignment
        iot_mock.add_thing_to_thing_group.assert_not_called()

    def test_missing_device_id_skips(self):
        dynamo_mock, table_mock = _make_dynamo_mock()
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler({"globalInstalledVersion": "1.0.0"}, None)

        table_mock.query.assert_not_called()
        table_mock.update_item.assert_not_called()

    def test_device_not_in_db_skips(self):
        dynamo_mock, table_mock = _make_dynamo_mock(existing_item=None)
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(), None)

        table_mock.update_item.assert_not_called()
        iot_mock.add_thing_to_thing_group.assert_not_called()


# ── Device model resolution ───────────────────────────────────────────────────

class TestDeviceModelResolution:
    def test_model_from_event_used(self):
        dynamo_mock, table_mock = _make_dynamo_mock(_existing(has_thing=False))
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(device_model="DGW-100"), None)

        # Should call add to DGW-100
        group_calls = [c.kwargs.get("thingGroupName") or c.args[0] if c.args else None
                       for c in iot_mock.add_thing_to_thing_group.call_args_list]
        assert "DGW-100" in group_calls or any("DGW-100" in str(c) for c in iot_mock.add_thing_to_thing_group.call_args_list)

    def test_model_from_db_used_when_not_in_event(self):
        dynamo_mock, table_mock = _make_dynamo_mock(
            _existing(has_thing=False, device_model="TP-100")
        )
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(), None)  # no deviceModel in event

        calls_str = str(iot_mock.add_thing_to_thing_group.call_args_list)
        assert "TP-100" in calls_str

    def test_event_model_overrides_db_model(self):
        dynamo_mock, table_mock = _make_dynamo_mock(
            _existing(has_thing=False, device_model="TP-100")
        )
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(device_model="DGW-100"), None)  # event wins

        calls_str = str(iot_mock.add_thing_to_thing_group.call_args_list)
        assert "DGW-100" in calls_str
        assert "TP-100" not in calls_str

    def test_no_model_falls_back_to_production(self):
        dynamo_mock, table_mock = _make_dynamo_mock(
            _existing(has_thing=False, device_model=None)
        )
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(), None)

        calls_str = str(iot_mock.add_thing_to_thing_group.call_args_list)
        assert lf.PRODUCTION_GROUP in calls_str

    def test_model_stored_in_db_when_present(self):
        dynamo_mock, table_mock = _make_dynamo_mock(_existing(has_thing=False))
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(device_model="DGW-200"), None)

        update_call_args = table_mock.update_item.call_args
        assert ":dm" in update_call_args.kwargs.get("ExpressionAttributeValues", {})

    def test_model_not_stored_when_absent(self):
        dynamo_mock, table_mock = _make_dynamo_mock(_existing(has_thing=False))
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(), None)

        update_call_args = table_mock.update_item.call_args
        assert ":dm" not in update_call_args.kwargs.get("ExpressionAttributeValues", {})


# ── Thing group hierarchy placement ──────────────────────────────────────────

class TestThingGroupHierarchy:
    def _run(self, device_model):
        dynamo_mock, _ = _make_dynamo_mock(_existing(has_thing=False))
        iot_mock = _make_iot_mock(canary_count=99)  # canary full → skip canary
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            result = lf._assign_to_thing_group(THING, device_model)
        return result, iot_mock

    def test_gateway_dwg100_placed_in_leaf(self):
        result, iot_mock = self._run("DGW-100")
        assert "DGW-100" in result
        assert lf.GATEWAYS_GROUP in result
        assert lf.PRODUCTION_GROUP in result
        assert lf.ROOT_GROUP in result

    def test_gateway_dwg200_placed_in_leaf(self):
        result, iot_mock = self._run("DGW-200")
        assert "DGW-200" in result

    def test_touchpanel_tp100_placed_in_leaf(self):
        result, iot_mock = self._run("TP-100")
        assert "TP-100" in result
        assert lf.TOUCH_PANELS_GROUP in result

    def test_touchpanel_tp200_placed_in_leaf(self):
        result, iot_mock = self._run("TP-200")
        assert "TP-200" in result

    def test_unknown_model_added_to_production(self):
        result, iot_mock = self._run("UNKNOWN-XYZ")
        assert lf.PRODUCTION_GROUP in result
        assert "UNKNOWN-XYZ" not in result

    def test_case_insensitive_model_matching(self):
        dynamo_mock, _ = _make_dynamo_mock(_existing(has_thing=False))
        iot_mock = _make_iot_mock(canary_count=99)
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            result = lf._assign_to_thing_group(THING, "dgw-100")  # lowercase
        assert "DGW-100" in result

    def test_add_to_thing_group_called_with_leaf(self):
        dynamo_mock, _ = _make_dynamo_mock(_existing(has_thing=False))
        iot_mock = _make_iot_mock(canary_count=99)
        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf._assign_to_thing_group(THING, "DGW-100")
        add_calls = [c.kwargs for c in iot_mock.add_thing_to_thing_group.call_args_list]
        groups_added = [c.get("thingGroupName") for c in add_calls]
        assert "DGW-100" in groups_added
        # Must NOT add to PRODUCTION_GROUP directly (device is in it via hierarchy)
        assert lf.PRODUCTION_GROUP not in groups_added


# ── Hierarchy creation ────────────────────────────────────────────────────────

class TestHierarchyCreation:
    def test_ensure_group_exists_creates_group(self):
        iot_mock = _make_iot_mock()
        with patch.object(lf, "iot", iot_mock):
            result = lf._ensure_group_exists("DIGILUX")
        assert result is True
        iot_mock.create_thing_group.assert_called_once_with(thingGroupName="DIGILUX")

    def test_ensure_group_exists_with_parent(self):
        iot_mock = _make_iot_mock()
        with patch.object(lf, "iot", iot_mock):
            result = lf._ensure_group_exists("GATEWAYS", "PRODUCTION")
        assert result is True
        iot_mock.create_thing_group.assert_called_once_with(
            thingGroupName="GATEWAYS", parentGroupName="PRODUCTION"
        )

    def test_ensure_group_already_exists_no_error(self):
        iot_mock = _make_iot_mock()
        iot_mock.create_thing_group.side_effect = _client_error("ResourceAlreadyExistsException")
        with patch.object(lf, "iot", iot_mock):
            result = lf._ensure_group_exists("DIGILUX")
        assert result is True  # silently ok

    def test_ensure_group_other_error_returns_false(self):
        iot_mock = _make_iot_mock()
        iot_mock.create_thing_group.side_effect = _client_error("AccessDeniedException")
        with patch.object(lf, "iot", iot_mock):
            result = lf._ensure_group_exists("DIGILUX")
        assert result is False

    def test_ensure_hierarchy_creates_all_groups(self):
        iot_mock = _make_iot_mock()
        with patch.object(lf, "iot", iot_mock):
            lf._ensure_hierarchy()

        create_calls = [c.kwargs.get("thingGroupName") for c in iot_mock.create_thing_group.call_args_list]
        assert lf.ROOT_GROUP           in create_calls
        assert lf.PRODUCTION_GROUP     in create_calls
        assert lf.GATEWAYS_GROUP       in create_calls
        assert lf.TOUCH_PANELS_GROUP   in create_calls
        # Leaf groups
        for model in lf.GATEWAY_MODELS | lf.TOUCH_PANEL_MODELS:
            assert model in create_calls

    def test_hierarchy_error_does_not_block_registration(self):
        """If _ensure_hierarchy raises, device still gets assigned."""
        dynamo_mock, table_mock = _make_dynamo_mock(_existing(has_thing=False))
        iot_mock = _make_iot_mock()
        # Make hierarchy creation blow up
        iot_mock.create_thing_group.side_effect = Exception("network error")

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            # Should not raise
            lf.lambda_handler(_event(device_model="DGW-100"), None)

        assert table_mock.update_item.called  # DB was still updated


# ── Fallback behaviour ────────────────────────────────────────────────────────

class TestFallbackBehaviour:
    def test_leaf_add_fails_falls_back_to_production(self):
        dynamo_mock, _ = _make_dynamo_mock(_existing(has_thing=False))
        iot_mock = _make_iot_mock(canary_count=99)

        call_count = {"n": 0}

        def add_side_effect(**kwargs):
            group = kwargs.get("thingGroupName", "")
            if group == "DGW-100":
                raise Exception("group error")
            return {}

        iot_mock.add_thing_to_thing_group.side_effect = add_side_effect

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            result = lf._assign_to_thing_group(THING, "DGW-100")

        # Fallback: PRODUCTION_GROUP should be in result
        assert lf.PRODUCTION_GROUP in result

    def test_canary_group_full_not_added(self):
        dynamo_mock, _ = _make_dynamo_mock(_existing(has_thing=False))
        iot_mock = _make_iot_mock(canary_count=lf.CANARY_MAX)  # exactly at limit

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            result = lf._assign_to_thing_group(THING, "DGW-100")

        assert lf.CANARY_GROUP not in result

    def test_canary_group_not_full_added(self):
        dynamo_mock, _ = _make_dynamo_mock(_existing(has_thing=False))
        iot_mock = _make_iot_mock(canary_count=0)  # empty canary

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            result = lf._assign_to_thing_group(THING, "DGW-100")

        assert lf.CANARY_GROUP in result


# ── resolve_leaf_group ────────────────────────────────────────────────────────

class TestResolveLeafGroup:
    def test_gateway_model(self):
        leaf, cat = lf._resolve_leaf_group("DGW-100")
        assert leaf == "DGW-100"
        assert cat == lf.GATEWAYS_GROUP

    def test_touch_panel_model(self):
        leaf, cat = lf._resolve_leaf_group("TP-100")
        assert leaf == "TP-100"
        assert cat == lf.TOUCH_PANELS_GROUP

    def test_unknown_model(self):
        leaf, cat = lf._resolve_leaf_group("UNKNOWN")
        assert leaf is None
        assert cat is None

    def test_empty_model(self):
        leaf, cat = lf._resolve_leaf_group("")
        assert leaf is None
        assert cat is None

    def test_case_insensitive(self):
        leaf, cat = lf._resolve_leaf_group("dgw-200")
        assert leaf == "DGW-200"

    def test_all_gateway_models_resolved(self):
        for model in lf.GATEWAY_MODELS:
            leaf, cat = lf._resolve_leaf_group(model)
            assert leaf == model
            assert cat == lf.GATEWAYS_GROUP

    def test_all_touch_panel_models_resolved(self):
        for model in lf.TOUCH_PANEL_MODELS:
            leaf, cat = lf._resolve_leaf_group(model)
            assert leaf == model
            assert cat == lf.TOUCH_PANELS_GROUP


# ── DynamoDB field correctness ────────────────────────────────────────────────

class TestDynamoUpdates:
    def test_global_installed_version_stored(self):
        dynamo_mock, table_mock = _make_dynamo_mock(_existing(has_thing=True))
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(version="2.5.0"), None)

        vals = table_mock.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert vals[":iv"] == "2.5.0"

    def test_thing_name_equals_device_id(self):
        dynamo_mock, table_mock = _make_dynamo_mock(_existing(has_thing=True))
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(), None)

        vals = table_mock.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert vals[":tn"] == DEVICE_ID

    def test_pending_job_id_not_overwritten(self):
        """pendingJobId = if_not_exists(...) so existing value must not be cleared."""
        dynamo_mock, table_mock = _make_dynamo_mock(_existing(has_thing=True))
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(), None)

        expr = table_mock.update_item.call_args.kwargs["UpdateExpression"]
        assert "if_not_exists(pendingJobId" in expr


# ── Audit logs ────────────────────────────────────────────────────────────────

class TestAuditLogs:
    def test_first_registration_audit(self, capsys):
        dynamo_mock, _ = _make_dynamo_mock(_existing(has_thing=False))
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(), None)

        out = capsys.readouterr().out
        audit_lines = [l for l in out.splitlines() if '"audit": true' in l.lower() or '"audit":true' in l]
        events = []
        for line in audit_lines:
            try:
                events.append(json.loads(line).get("event", ""))
            except Exception:
                pass
        assert "DEVICE_FIRST_REGISTRATION" in events

    def test_reconnect_audit(self, capsys):
        dynamo_mock, _ = _make_dynamo_mock(_existing(has_thing=True))
        iot_mock = _make_iot_mock()

        with patch.object(lf, "dynamo", dynamo_mock), patch.object(lf, "iot", iot_mock):
            lf.lambda_handler(_event(), None)

        out = capsys.readouterr().out
        audit_lines = [l for l in out.splitlines() if '"audit"' in l]
        events = []
        for line in audit_lines:
            try:
                events.append(json.loads(line).get("event", ""))
            except Exception:
                pass
        assert "DEVICE_RECONNECTED" in events
