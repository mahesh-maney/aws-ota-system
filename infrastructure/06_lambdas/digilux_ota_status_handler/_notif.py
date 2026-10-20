"""
_notif.py — shared email notification helper for OTA Lambdas.
Copy this file into each Lambda directory that requires it.

Reads from env vars:
  SES_SENDER    — verified SES from-address (e.g. noreply@iot.digilux.co.in)
  ADMIN_EMAILS  — comma-separated admin addresses (e.g. ops@digilux.co.in,cto@digilux.co.in)
  REGION        — AWS region for SES client
"""
import json
import logging
import os

log = logging.getLogger(__name__)


# ── SES helpers ───────────────────────────────────────────────────────────────

def _ses():
    import boto3
    return boto3.client("ses", region_name=os.environ.get("REGION", "ap-south-1"))


def _sender():
    return os.environ.get("SES_SENDER", "")


def _admin_emails():
    raw = os.environ.get("ADMIN_EMAILS", "")
    return [e.strip() for e in raw.split(",") if e.strip()]


def send_email(to_addresses, subject, body_text):
    """Send a plain-text SES email. Non-fatal — logs warning on failure."""
    sender = _sender()
    if not sender or not to_addresses:
        log.debug(json.dumps({"msg": "email_skipped",
                              "reason": "no sender or recipients",
                              "subject": subject}))
        return
    try:
        _ses().send_email(
            Source=sender,
            Destination={"ToAddresses": list(to_addresses)},
            Message={
                "Subject": {"Data": subject,    "Charset": "UTF-8"},
                "Body":    {"Text": {"Data": body_text, "Charset": "UTF-8"}},
            },
        )
        log.info(json.dumps({"msg": "email_sent", "subject": subject,
                              "to": list(to_addresses)}))
    except Exception as e:
        log.warning(json.dumps({"msg": "email_send_failed", "subject": subject,
                                "to": list(to_addresses), "error": str(e)}))


def notify_admin(subject, body_text):
    """Send email to all addresses in ADMIN_EMAILS env var."""
    send_email(_admin_emails(), subject, body_text)


def get_user_email(cognito_client, pool_id, user_id):
    """Look up a user's email from Cognito by userId (sub). Returns None on failure."""
    if not pool_id or not user_id:
        return None
    try:
        resp = cognito_client.admin_get_user(UserPoolId=pool_id, Username=user_id)
        for attr in resp.get("UserAttributes", []):
            if attr["Name"] == "email":
                return attr["Value"]
    except Exception as e:
        log.warning(json.dumps({"msg": "cognito_email_lookup_failed",
                                "userId": user_id, "error": str(e)}))
    return None


# ── Email templates ───────────────────────────────────────────────────────────

def tmpl_deployment_created(package_name, version, rollout_stage,
                             target_id, deployment_id, created_by):
    subject = f"[OTA] New deployment created — {package_name} v{version} ({rollout_stage})"
    body = f"""A new OTA deployment has been created.

Package    : {package_name}
Version    : {version}
Stage      : {rollout_stage}
Target     : {target_id}
Deployment : {deployment_id}
Created by : {created_by}

Devices in the target will be offered this update when they next check in.
"""
    return subject, body


def tmpl_deployment_aborted_admin(package_name, version, rollout_stage,
                                   deployment_id, aborted_by, reason,
                                   cancelled_jobs, failed_jobs):
    subject = f"[OTA] Deployment aborted — {package_name} v{version} ({rollout_stage})"
    body = f"""An OTA deployment has been aborted by an administrator.

Package              : {package_name}
Version              : {version}
Stage                : {rollout_stage}
Deployment           : {deployment_id}
Aborted by           : {aborted_by}
Reason               : {reason}
IoT jobs cancelled   : {cancelled_jobs}
IoT jobs failed to cancel: {failed_jobs}

Devices that had not yet started downloading will no longer receive this update.
"""
    return subject, body


def tmpl_deployment_aborted_user(package_name, version, reason):
    subject = f"[OTA] Your firmware update has been cancelled — {package_name} v{version}"
    body = f"""Your pending firmware update has been cancelled by an administrator.

Package : {package_name}
Version : {version}
Reason  : {reason}

No action is required. A new update may be offered in the future.
"""
    return subject, body


def tmpl_deployment_superseded(package_name, old_version, new_version,
                                rollout_stage, old_dep_id, new_dep_id):
    subject = f"[OTA] Deployment superseded — {package_name} ({rollout_stage})"
    body = f"""An OTA deployment has been superseded by a newer version.

Package      : {package_name}
Old version  : {old_version}  (id: {old_dep_id})
New version  : {new_version}  (id: {new_dep_id})
Stage        : {rollout_stage}

Devices will now be offered the new version instead.
"""
    return subject, body


def tmpl_install_succeeded(package_name, version, thing_name, device_id):
    subject = f"[OTA] Firmware update successful — {package_name} v{version}"
    body = f"""Your device has successfully installed the firmware update.

Package : {package_name}
Version : {version}
Device  : {thing_name or device_id}

No further action is required.
"""
    return subject, body


def tmpl_install_failed_user(package_name, version, thing_name, device_id,
                              error_code, error_reason):
    subject = f"[OTA] Firmware update failed — {package_name} v{version}"
    body = f"""Your device encountered an error while installing the firmware update.

Package      : {package_name}
Version      : {version}
Device       : {thing_name or device_id}
Error code   : {error_code or 'N/A'}
Error reason : {error_reason or 'N/A'}

Please contact support if the issue persists.
"""
    return subject, body


def tmpl_install_failed_admin(package_name, version, thing_name, device_id,
                               job_id, error_code, error_reason):
    subject = f"[OTA] ALERT: Install failed — {package_name} v{version} on {thing_name or device_id}"
    body = f"""A device has failed to install a firmware update.

Package      : {package_name}
Version      : {version}
Device       : {thing_name or device_id}  (id: {device_id})
Job          : {job_id}
Error code   : {error_code or 'N/A'}
Error reason : {error_reason or 'N/A'}

Review CloudWatch logs for details.
"""
    return subject, body


def tmpl_install_timed_out_user(package_name, version, thing_name, device_id):
    subject = f"[OTA] Firmware update timed out — {package_name} v{version}"
    body = f"""Your firmware update timed out before completing.

Package : {package_name}
Version : {version}
Device  : {thing_name or device_id}

This may be due to the device being offline for an extended period.
Please contact support if you believe this is an error.
"""
    return subject, body


def tmpl_install_timed_out_admin(package_name, version, thing_name, device_id, job_id):
    subject = f"[OTA] ALERT: Install timed out — {package_name} v{version} on {thing_name or device_id}"
    body = f"""A device firmware update has timed out.

Package : {package_name}
Version : {version}
Device  : {thing_name or device_id}  (id: {device_id})
Job     : {job_id}

The device may have been offline too long. Review CloudWatch logs for details.
"""
    return subject, body


def tmpl_artifact_active(package_name, version, device_type, artifact_size, uploaded_by):
    subject = f"[OTA] Artifact ready — {package_name} v{version}"
    body = f"""A new OTA artifact has been processed successfully and is ready for deployment.

Package     : {package_name}
Version     : {version}
Device type : {device_type}
Size        : {artifact_size:,} bytes
Uploaded by : {uploaded_by}

You can now create a deployment from the admin UI.
"""
    return subject, body


def tmpl_artifact_corrupted(package_name, version, device_type, reason, uploaded_by):
    subject = f"[OTA] ALERT: Artifact corrupted — {package_name} v{version}"
    body = f"""An uploaded OTA artifact failed integrity checks and has been marked CORRUPTED.

Package     : {package_name}
Version     : {version}
Device type : {device_type}
Reason      : {reason}
Uploaded by : {uploaded_by}

The artifact cannot be deployed. Please re-upload a valid artifact.
"""
    return subject, body


def tmpl_stale_jobs(stale_jobs, threshold_days):
    subject = f"[OTA] Stale job alert — {len(stale_jobs)} job(s) auto-cancelled"
    lines = "\n".join(
        f"  - {j.get('jobId','?')}  package={j.get('packageName','?')}  "
        f"status={j.get('status','?')}  device={j.get('thingName', j.get('deviceId','?'))}"
        for j in stale_jobs
    )
    body = f"""The daily OTA job sync auto-cancelled {len(stale_jobs)} stale job(s) \
older than {threshold_days} days.

{lines}

These devices may be offline or stuck. Review and take action if needed.
"""
    return subject, body


def tmpl_package_recalled_admin(package_name, version, recalled_by, reason,
                                 cancelled_jobs, in_progress_jobs):
    subject = f"[OTA] Package recalled — {package_name} v{version}"
    ip_lines = "\n".join(f"  - {j}" for j in in_progress_jobs) if in_progress_jobs else "  None"
    ip_section = ("⚠ In-progress jobs (require manual action):\n" + ip_lines) if in_progress_jobs else ""
    body = f"""An OTA package has been recalled and will no longer be offered to devices.

Package                : {package_name}
Version                : {version}
Recalled by            : {recalled_by}
Reason                 : {reason}
Jobs cancelled         : {len(cancelled_jobs)}
Jobs still in progress : {len(in_progress_jobs)}
{ip_section}
"""
    return subject, body


def tmpl_package_recalled_user(package_name, version, reason):
    subject = f"[OTA] Your firmware update has been recalled — {package_name} v{version}"
    body = f"""An OTA firmware update pending for your device has been recalled.

Package : {package_name}
Version : {version}
Reason  : {reason}

No action is required. A safe replacement may be offered in the future.
"""
    return subject, body


def tmpl_package_deleted(package_name, version, device_type, deleted_by, reason):
    subject = f"[OTA] Package deleted — {package_name} v{version}"
    body = f"""An OTA package has been permanently deleted.

Package     : {package_name}
Version     : {version}
Device type : {device_type}
Deleted by  : {deleted_by}
Reason      : {reason}
"""
    return subject, body
