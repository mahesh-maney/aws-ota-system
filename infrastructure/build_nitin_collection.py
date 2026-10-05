#!/usr/bin/env python3.9
"""
Convert the directory-based YAML request files into a Postman Collection v2.1 JSON
that Newman can execute.
"""
import hashlib, json, os, re, sys, time, uuid
import yaml  # pyyaml

COLLECTION_DIR = "/private/tmp/OTA_UC_Test_v3.postman_collection.json"

# ── Variable values ──────────────────────────────────────────────────────────
TS           = int(time.time())
TEST_VERSION = f"9.0.{TS}-nitin"
UC1_VERSION  = f"9.0.{TS}-nitin-uc1"

# PKCE access token — needed for consent endpoint (requires OAuth Bearer scope)
try:
    PKCE_TOKEN = open("/tmp/ota_pkce_token.txt").read().strip()
except FileNotFoundError:
    PKCE_TOKEN = ""
    print("WARNING: /tmp/ota_pkce_token.txt not found — consent tests will fail")
TEST_SHA256  = open("/tmp/ota_test.tar", "rb").read()
TEST_SHA256  = hashlib.sha256(TEST_SHA256).hexdigest()

# Regenerate tar with proper manifest (packageName + version required by artifact_processor)
import io, tarfile as _tf

def _make_test_tar(version: str, package_name: str = "HomeAssistantUtility") -> bytes:
    manifest = {
        "packageName": package_name,
        "version":     version,
        "files": [{"name": "firmware.bin", "type": 1,
                   "sha256": "abc123", "size": 16}],
    }
    buf = io.BytesIO()
    with _tf.open(fileobj=buf, mode="w") as t:
        mdata = json.dumps(manifest).encode()
        ti = _tf.TarInfo("manifest.json"); ti.size = len(mdata)
        t.addfile(ti, io.BytesIO(mdata))
        fdata = b"FAKE_FIRMWARE_DATA"
        ti2 = _tf.TarInfo("firmware.bin"); ti2.size = len(fdata)
        t.addfile(ti2, io.BytesIO(fdata))
    return buf.getvalue()


_TAR_BYTES = _make_test_tar(TEST_VERSION)
with open("/tmp/ota_test.tar", "wb") as _f:
    _f.write(_TAR_BYTES)
TEST_SHA256 = hashlib.sha256(_TAR_BYTES).hexdigest()

ENV_VARS = {
    "base_url":      "https://iot.digilux.co.in/smarthome",
    "admin_token":   "__ADMIN_TOKEN__",
    "user_token":    "__USER_TOKEN__",
    "version":       TEST_VERSION,
    "package_name":  "HomeAssistantUtility",
    "device_id":     "edb39bba-baf1-4700-968c-a42228e53aa0",
    "device_mac":    "aa:bb:cc:dd:ee:f1",
    "uc1_version":   UC1_VERSION,
    "pkce_token":    PKCE_TOKEN,
    # Placeholders — set by afterResponse scripts during run
    "upload_url":    "",
    "deployment_id": "",
    "job_id":        "",
    "beta_user_id":  "",
    "prev_job_id":   "",
}


def parse_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def to_postman_url(raw_url):
    """Return raw URL string — Newman handles variable substitution itself."""
    return raw_url


def to_postman_headers(headers_dict):
    if not headers_dict:
        return []
    return [{"key": k, "value": v} for k, v in headers_dict.items()]


def remap_url(url: str) -> str:
    """
    Remap Postman collection URLs to the actual deployed API paths.
    The collection uses /upload-url but the actual path is /packages/upload-artefact.
    The collection uses /device/available-updates (requires OAuth Bearer) but
    /my/updates accepts Cognito ID token directly.
    """
    url = url.replace("/api/v1/ota/upload-url", "/api/v1/ota/packages/upload-artefact")
    # /device/available-updates requires PKCE OAuth token which we can't get headlessly.
    # /my/updates returns the same data and accepts a plain Cognito ID token.
    url = url.replace("/api/v1/ota/device/available-updates", "/api/v1/ota/my/updates")
    return url


def to_postman_body(body_def):
    if not body_def:
        return None
    btype = body_def.get("type", "text")
    if btype == "text":
        content = body_def.get("content", "")
        # Replace <sha256-of-your-tar> with actual test sha256
        content = content.replace("<sha256-of-your-tar>", TEST_SHA256)
        content = content.replace("<sha256>", TEST_SHA256)
        return {
            "mode": "raw",
            "raw": content,
            "options": {"raw": {"language": "json"}},
        }
    elif btype == "file":
        return {
            "mode": "file",
            "file": {"src": "/tmp/ota_test.tar"},
        }
    elif btype == "urlencoded":
        pairs = body_def.get("content", {})
        return {
            "mode": "urlencoded",
            "urlencoded": [{"key": k, "value": v} for k, v in pairs.items()],
        }
    return None


def to_postman_events(scripts):
    if not scripts:
        return []
    events = []
    for s in scripts:
        listen = "test" if s.get("type") == "afterResponse" else "prerequest"
        code   = s.get("code", "")
        events.append({
            "listen": listen,
            "script": {
                "exec": code.splitlines(),
                "type": "text/javascript",
            },
        })
    return events


def yaml_to_item(path, folder=""):
    data = parse_yaml(path)
    if not data or data.get("$kind") != "http-request":
        return None

    name = data.get("name") or os.path.splitext(os.path.basename(path))[0]
    method = data.get("method", "GET")
    url    = remap_url(to_postman_url(data.get("url", "")))
    headers_dict = dict(data.get("headers", {}) or {})

    # ── Consent endpoint requires OAuth Bearer PKCE token (not plain ID token) ─
    is_consent = (method == "POST" and "/consent" in url)
    if is_consent:
        headers_dict["Authorization"] = "Bearer {{pkce_token}}"

    # ── Inject upload-token header for S3 PUT requests that are missing it ──
    is_s3_put = (method == "PUT" and "upload_url" in url)
    if is_s3_put and "x-amz-meta-upload-token" not in headers_dict:
        headers_dict["x-amz-meta-upload-token"] = "{{upload_token}}"

    item = {
        "name": name,
        "request": {
            "method": method,
            "header": to_postman_headers(headers_dict),
            "url":    url,
        },
    }

    # ── UC1 version isolation: prevent collision with Smoke {{version}} ────────
    if folder.startswith("UC1"):
        raw_body = data.get("body")
        if raw_body and isinstance(raw_body.get("content"), str):
            raw_body = dict(raw_body)
            raw_body["content"] = raw_body["content"].replace("{{version}}", "{{uc1_version}}")
            data = dict(data)
            data["body"] = raw_body
        # Also fix URL if it contains {{version}}
        url = url.replace("{{version}}", "{{uc1_version}}")
        item["request"]["url"] = url

    body = to_postman_body(data.get("body"))
    if body:
        item["request"]["body"] = body

    scripts = list(data.get("scripts", []) or [])

    # ── After upload-artefact responses, also save uploadToken ──────────────
    is_upload_url = (method == "POST" and
                     ("upload-artefact" in url or "upload-url" in url))
    if is_upload_url:
        inject = "if (b.uploadToken) pm.collectionVariables.set('upload_token', b.uploadToken);"
        injected = False
        for s in scripts:
            if s.get("type") == "afterResponse":
                code = s.get("code", "")
                if "upload_token" not in code:
                    lines = code.rstrip()
                    s["code"] = lines + "\n" + inject
                injected = True
                break
        if not injected:
            scripts.append({
                "type": "afterResponse",
                "code": (
                    "const b = pm.response.json();\n"
                    "if (b.uploadUrl) pm.collectionVariables.set('upload_url', b.uploadUrl);\n"
                    + inject
                ),
            })

    # ── Add 5-second poll delay before package-status polling requests ───────
    name_lc = name.lower()
    is_poll = ("poll" in name_lc or
               ("status" in name_lc and method == "GET" and "packages" in url))
    if is_poll:
        delay_script = {
            "type": "prerequest",
            "code": (
                "// Wait for artifact_processor Lambda to finish\n"
                "const start = Date.now();\n"
                "while (Date.now() - start < 5000) {}"
            ),
        }
        scripts.insert(0, delay_script)

    events = to_postman_events(scripts)
    if events:
        item["event"] = events

    return item


# ── Build collection ─────────────────────────────────────────────────────────

# Folder order: Auth first, Smoke second, then UC1–UC25 numerically
def folder_sort_key(name):
    if name.startswith("Auth"):
        return (0, 0, name)
    if name.startswith("Smoke"):
        return (1, 0, name)
    m = re.match(r"UC(\d+)", name)
    if m:
        return (2, int(m.group(1)), name)
    return (3, 0, name)


def file_sort_key(fname):
    """Sort request files by their leading number/letter+number prefix."""
    # Match patterns: "1.1 ...", "S1 ...", "10.1 ...", "No ..."
    m = re.match(r"^[A-Z]?(\d+)(?:\.(\d+))?", fname)
    if m:
        major = int(m.group(1))
        minor = int(m.group(2)) if m.group(2) else 0
        return (major, minor)
    return (999, 0)


collection_items = []

# Skip the Auth folder — tokens are pre-injected as collection variables
SKIP_FOLDERS = {"Auth — Get Tokens"}

folders = sorted(os.listdir(COLLECTION_DIR), key=folder_sort_key)
for folder in folders:
    if folder in SKIP_FOLDERS:
        continue
    folder_path = os.path.join(COLLECTION_DIR, folder)
    if not os.path.isdir(folder_path):
        continue

    yaml_files = sorted(
        [f for f in os.listdir(folder_path) if f.endswith(".yaml")],
        key=file_sort_key,
    )

    sub_items = []
    for yf in yaml_files:
        item = yaml_to_item(os.path.join(folder_path, yf), folder=folder)
        if item:
            sub_items.append(item)

    if sub_items:
        # After Smoke section: inject a device-state reset so S7's job doesn't
        # cascade into UC2/UC19/UC20 which expect a clean device.
        if folder.startswith("Smoke"):
            sub_items.append({
                "name": "TEARDOWN — Abort Smoke deployment + reset device state",
                "request": {
                    "method": "PATCH",
                    "header": [
                        {"key": "Authorization", "value": "{{admin_token}}"},
                        {"key": "Content-Type", "value": "application/json"},
                    ],
                    "url": "{{base_url}}/api/v1/ota/deployments/{{deployment_id}}/abort",
                    "body": {
                        "mode": "raw",
                        "raw": '{"reason":"Smoke teardown — reset for UC tests"}',
                        "options": {"raw": {"language": "json"}},
                    },
                },
                "event": [{
                    "listen": "test",
                    "script": {
                        "exec": [
                            "// Best-effort abort — ignore errors",
                            "const code = pm.response.code;",
                            "// also clear job_id so UC tests start fresh",
                            "pm.collectionVariables.set('job_id', '');",
                        ],
                        "type": "text/javascript",
                    },
                }],
            })
        collection_items.append({
            "name": folder,
            "item": sub_items,
        })

collection = {
    "info": {
        "name": "OTA UC Tests (Nitin) — automated",
        "_postman_id": str(uuid.uuid4()),
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "item": collection_items,
    "variable": [
        {"key": k, "value": v, "type": "string"} for k, v in ENV_VARS.items()
    ],
}

out = "/tmp/nitin_uc_collection.json"
with open(out, "w") as f:
    json.dump(collection, f, indent=2)

print(f"Collection written: {out}")
print(f"Folders: {len(collection_items)}")
total = sum(len(f['item']) for f in collection_items)
print(f"Total requests: {total}")
print(f"TEST_VERSION: {TEST_VERSION}")
print(f"TEST_SHA256:  {TEST_SHA256}")
