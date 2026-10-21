"""
Test suite for _notif.py — shared SES email notification helper.

Coverage:
  send_email
    - Positive: SES called with correct Source, Destination, Message
    - No sender env var → skip (no SES call)
    - Empty recipient list → skip
    - SES raises → log warning, no exception propagated

  notify_admin
    - ADMIN_EMAILS set → send_email called for each address
    - ADMIN_EMAILS empty → no email sent

  get_user_email
    - Positive: returns email attribute from Cognito response
    - Empty pool_id → None (no Cognito call)
    - Empty user_id → None
    - Cognito raises → None, no exception propagated

  Template spot-checks (subject/body contain key fields)
    - tmpl_deployment_created
    - tmpl_deployment_aborted_admin
    - tmpl_deployment_aborted_user
    - tmpl_deployment_superseded
    - tmpl_install_succeeded
    - tmpl_install_failed_user
    - tmpl_artifact_active
    - tmpl_artifact_corrupted
    - tmpl_stale_jobs
    - tmpl_package_recalled_admin
    - tmpl_package_recalled_user
    - tmpl_package_deleted

Run:
  pytest infrastructure/tests/test_notif.py -v
"""
import os
from unittest.mock import MagicMock, patch

import pytest

# ── env setup ────────────────────────────────────────────────────────────────
os.environ.setdefault("REGION", "ap-south-1")
os.environ.setdefault("SES_SENDER", "")
os.environ.setdefault("ADMIN_EMAILS", "")

import _notif  # noqa: E402  (conftest.py adds 06_lambdas/ to sys.path)


# ── helpers ───────────────────────────────────────────────────────────────────

def _with_env(**kw):
    """Context manager: temporarily set env vars for the test."""
    return patch.dict(os.environ, kw)


# ── send_email ────────────────────────────────────────────────────────────────

class TestSendEmail:
    def test_ses_called_with_correct_params(self):
        ses_mock = MagicMock()
        with _with_env(SES_SENDER="noreply@example.com"):
            with patch.object(_notif, "_ses", return_value=ses_mock):
                _notif.send_email(["user@example.com"], "Test subject", "Test body")
        ses_mock.send_email.assert_called_once()
        call_kwargs = ses_mock.send_email.call_args[1]
        assert call_kwargs["Source"] == "noreply@example.com"
        assert call_kwargs["Destination"]["ToAddresses"] == ["user@example.com"]
        assert call_kwargs["Message"]["Subject"]["Data"] == "Test subject"
        assert call_kwargs["Message"]["Body"]["Text"]["Data"] == "Test body"

    def test_multiple_recipients(self):
        ses_mock = MagicMock()
        recipients = ["a@example.com", "b@example.com"]
        with _with_env(SES_SENDER="noreply@example.com"):
            with patch.object(_notif, "_ses", return_value=ses_mock):
                _notif.send_email(recipients, "subj", "body")
        sent_to = ses_mock.send_email.call_args[1]["Destination"]["ToAddresses"]
        assert set(sent_to) == set(recipients)

    def test_no_sender_skips_ses(self):
        ses_mock = MagicMock()
        with _with_env(SES_SENDER=""):
            with patch.object(_notif, "_ses", return_value=ses_mock):
                _notif.send_email(["user@example.com"], "subj", "body")
        ses_mock.send_email.assert_not_called()

    def test_empty_recipients_skips_ses(self):
        ses_mock = MagicMock()
        with _with_env(SES_SENDER="noreply@example.com"):
            with patch.object(_notif, "_ses", return_value=ses_mock):
                _notif.send_email([], "subj", "body")
        ses_mock.send_email.assert_not_called()

    def test_ses_exception_is_non_fatal(self):
        """SES failure must never raise — only log a warning."""
        ses_mock = MagicMock()
        ses_mock.send_email.side_effect = Exception("SES unavailable")
        with _with_env(SES_SENDER="noreply@example.com"):
            with patch.object(_notif, "_ses", return_value=ses_mock):
                # Must not raise
                _notif.send_email(["user@example.com"], "subj", "body")


# ── notify_admin ──────────────────────────────────────────────────────────────

class TestNotifyAdmin:
    def test_sends_to_all_admin_emails(self):
        ses_mock = MagicMock()
        with _with_env(SES_SENDER="noreply@example.com",
                       ADMIN_EMAILS="ops@example.com,cto@example.com"):
            with patch.object(_notif, "_ses", return_value=ses_mock):
                _notif.notify_admin("Alert subject", "Alert body")
        ses_mock.send_email.assert_called_once()
        dest = ses_mock.send_email.call_args[1]["Destination"]["ToAddresses"]
        assert "ops@example.com" in dest
        assert "cto@example.com" in dest

    def test_empty_admin_emails_sends_nothing(self):
        ses_mock = MagicMock()
        with _with_env(SES_SENDER="noreply@example.com", ADMIN_EMAILS=""):
            with patch.object(_notif, "_ses", return_value=ses_mock):
                _notif.notify_admin("subj", "body")
        ses_mock.send_email.assert_not_called()

    def test_admin_emails_whitespace_stripped(self):
        """Spaces around commas must be trimmed."""
        ses_mock = MagicMock()
        with _with_env(SES_SENDER="noreply@example.com",
                       ADMIN_EMAILS=" ops@example.com , cto@example.com "):
            with patch.object(_notif, "_ses", return_value=ses_mock):
                _notif.notify_admin("subj", "body")
        dest = ses_mock.send_email.call_args[1]["Destination"]["ToAddresses"]
        assert "ops@example.com" in dest
        assert "cto@example.com" in dest


# ── get_user_email ────────────────────────────────────────────────────────────

class TestGetUserEmail:
    def test_returns_email_from_cognito(self):
        cognito = MagicMock()
        cognito.admin_get_user.return_value = {
            "UserAttributes": [
                {"Name": "sub",   "Value": "user-sub-123"},
                {"Name": "email", "Value": "user@example.com"},
            ]
        }
        result = _notif.get_user_email(cognito, "us-east-1_pool123", "user-sub-123")
        assert result == "user@example.com"
        cognito.admin_get_user.assert_called_once_with(
            UserPoolId="us-east-1_pool123", Username="user-sub-123"
        )

    def test_empty_pool_id_returns_none(self):
        cognito = MagicMock()
        result = _notif.get_user_email(cognito, "", "user-sub-123")
        assert result is None
        cognito.admin_get_user.assert_not_called()

    def test_empty_user_id_returns_none(self):
        cognito = MagicMock()
        result = _notif.get_user_email(cognito, "pool123", "")
        assert result is None
        cognito.admin_get_user.assert_not_called()

    def test_none_user_id_returns_none(self):
        cognito = MagicMock()
        result = _notif.get_user_email(cognito, "pool123", None)
        assert result is None

    def test_cognito_exception_returns_none(self):
        """Cognito failure must return None, never raise."""
        cognito = MagicMock()
        cognito.admin_get_user.side_effect = Exception("Cognito down")
        result = _notif.get_user_email(cognito, "pool123", "user-sub-123")
        assert result is None

    def test_no_email_attribute_returns_none(self):
        """User exists in Cognito but has no email attribute → None."""
        cognito = MagicMock()
        cognito.admin_get_user.return_value = {
            "UserAttributes": [{"Name": "sub", "Value": "user-sub-123"}]
        }
        result = _notif.get_user_email(cognito, "pool123", "user-sub-123")
        assert result is None


# ── Template spot-checks ──────────────────────────────────────────────────────

class TestTemplates:
    """Each template must return (subject, body) with key fields present."""

    def test_tmpl_deployment_created(self):
        subj, body = _notif.tmpl_deployment_created(
            "HomeAssistantUtility", "4.5.0", "PRODUCTION",
            "DGX-Gateways", "dep-001", "admin@example.com"
        )
        assert "HomeAssistantUtility" in subj
        assert "4.5.0" in subj
        assert "HomeAssistantUtility" in body
        assert "4.5.0" in body
        assert "dep-001" in body

    def test_tmpl_deployment_aborted_admin(self):
        subj, body = _notif.tmpl_deployment_aborted_admin(
            "HomeAssistantUtility", "4.5.0", "BETA",
            "dep-002", "admin@example.com", "Security issue",
            ["job-1"], []
        )
        assert "HomeAssistantUtility" in subj
        assert "4.5.0" in subj
        assert "dep-002" in body
        assert "Security issue" in body

    def test_tmpl_deployment_aborted_user(self):
        subj, body = _notif.tmpl_deployment_aborted_user(
            "HomeAssistantUtility", "4.5.0", "Security issue"
        )
        assert "HomeAssistantUtility" in subj
        assert "4.5.0" in subj
        assert "Security issue" in body

    def test_tmpl_deployment_superseded(self):
        subj, body = _notif.tmpl_deployment_superseded(
            "HomeAssistantUtility", "3.0.0", "4.5.0", "PRODUCTION",
            "dep-old", "dep-new"
        )
        assert "HomeAssistantUtility" in subj
        assert "dep-old" in body
        assert "dep-new" in body

    def test_tmpl_install_succeeded(self):
        subj, body = _notif.tmpl_install_succeeded(
            "HomeAssistantUtility", "4.5.0", "thing-001", "dev-001"
        )
        assert "HomeAssistantUtility" in subj
        assert "4.5.0" in subj
        assert "thing-001" in body

    def test_tmpl_install_failed_user(self):
        subj, body = _notif.tmpl_install_failed_user(
            "HomeAssistantUtility", "4.5.0", "thing-001", "dev-001",
            "ERR_CHECKSUM", "Checksum mismatch"
        )
        assert "HomeAssistantUtility" in subj
        assert "Checksum mismatch" in body

    def test_tmpl_install_failed_user_no_error_code(self):
        """None error code and reason should show N/A."""
        subj, body = _notif.tmpl_install_failed_user(
            "HomeAssistantUtility", "4.5.0", None, "dev-001", None, None
        )
        assert "N/A" in body

    def test_tmpl_artifact_active(self):
        subj, body = _notif.tmpl_artifact_active(
            "HomeAssistantUtility", "4.5.0", "Network_controller_firmware",
            1024000, "uploader@example.com"
        )
        assert "HomeAssistantUtility" in subj
        assert "4.5.0" in subj
        assert "1,024,000" in body  # formatted with commas

    def test_tmpl_artifact_corrupted(self):
        subj, body = _notif.tmpl_artifact_corrupted(
            "HomeAssistantUtility", "4.5.0", "Network_controller_firmware",
            "SHA256 mismatch", "uploader@example.com"
        )
        assert "CORRUPTED" in subj or "corrupted" in subj.lower()
        assert "SHA256 mismatch" in body

    def test_tmpl_stale_jobs(self):
        stale = [
            {"jobId": "job-001", "packageName": "HAU", "status": "QUEUED",
             "thingName": "thing-001"},
            {"jobId": "job-002", "packageName": "HAU", "status": "QUEUED",
             "deviceId": "dev-002"},
        ]
        subj, body = _notif.tmpl_stale_jobs(stale, 30)
        assert "2" in subj
        assert "job-001" in body
        assert "job-002" in body
        assert "30" in body

    def test_tmpl_package_recalled_admin_no_in_progress(self):
        subj, body = _notif.tmpl_package_recalled_admin(
            "HomeAssistantUtility", "4.5.0", "admin@example.com",
            "Defective firmware", ["job-1", "job-2"], []
        )
        assert "HomeAssistantUtility" in subj
        assert "Defective firmware" in body

    def test_tmpl_package_recalled_admin_with_in_progress(self):
        """In-progress jobs section appears when present."""
        subj, body = _notif.tmpl_package_recalled_admin(
            "HomeAssistantUtility", "4.5.0", "admin@example.com",
            "Defective firmware", ["job-1"], ["job-in-progress-1"]
        )
        assert "job-in-progress-1" in body

    def test_tmpl_package_recalled_user(self):
        subj, body = _notif.tmpl_package_recalled_user(
            "HomeAssistantUtility", "4.5.0", "Defective firmware"
        )
        assert "HomeAssistantUtility" in subj
        assert "Defective firmware" in body

    def test_tmpl_package_deleted(self):
        subj, body = _notif.tmpl_package_deleted(
            "HomeAssistantUtility", "4.5.0", "Network_controller_firmware",
            "admin@example.com", "Obsolete version"
        )
        assert "HomeAssistantUtility" in subj
        assert "4.5.0" in subj
        assert "Obsolete version" in body

    def test_all_templates_return_two_strings(self):
        """Each template function must return a (str, str) tuple."""
        templates_and_args = [
            (_notif.tmpl_deployment_created,
             ("pkg", "1.0", "PROD", "target", "dep-id", "actor")),
            (_notif.tmpl_deployment_aborted_admin,
             ("pkg", "1.0", "PROD", "dep-id", "actor", "reason", [], [])),
            (_notif.tmpl_deployment_aborted_user,
             ("pkg", "1.0", "reason")),
            (_notif.tmpl_deployment_superseded,
             ("pkg", "1.0", "2.0", "PROD", "dep-old", "dep-new")),
            (_notif.tmpl_install_succeeded,
             ("pkg", "1.0", "thing", "dev")),
            (_notif.tmpl_install_failed_user,
             ("pkg", "1.0", "thing", "dev", None, None)),
            (_notif.tmpl_install_failed_admin,
             ("pkg", "1.0", "thing", "dev", "job-1", None, None)),
            (_notif.tmpl_install_timed_out_user,
             ("pkg", "1.0", "thing", "dev")),
            (_notif.tmpl_install_timed_out_admin,
             ("pkg", "1.0", "thing", "dev", "job-1")),
            (_notif.tmpl_artifact_active,
             ("pkg", "1.0", "type", 1024, "uploader")),
            (_notif.tmpl_artifact_corrupted,
             ("pkg", "1.0", "type", "reason", "uploader")),
            (_notif.tmpl_stale_jobs,
             ([{"jobId": "j1", "packageName": "p", "status": "QUEUED",
                "thingName": "t"}], 30)),
            (_notif.tmpl_package_recalled_admin,
             ("pkg", "1.0", "actor", "reason", [], [])),
            (_notif.tmpl_package_recalled_user,
             ("pkg", "1.0", "reason")),
            (_notif.tmpl_package_deleted,
             ("pkg", "1.0", "type", "actor", "reason")),
        ]
        for fn, args in templates_and_args:
            result = fn(*args)
            assert isinstance(result, tuple) and len(result) == 2, \
                f"{fn.__name__} did not return a 2-tuple"
            subj, body = result
            assert isinstance(subj, str) and len(subj) > 0, \
                f"{fn.__name__} returned empty subject"
            assert isinstance(body, str) and len(body) > 0, \
                f"{fn.__name__} returned empty body"
