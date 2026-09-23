#!/usr/bin/env python3
"""
Obtain a Cognito PKCE access token for the Digilux device user pool.
Automates the Cognito Managed Login flow:
  GET /oauth2/authorize → POST username → POST password → auth-code redirect → token exchange

The access token (with smarthome_server/read + write scopes) is saved to
/tmp/ota_pkce_token.txt (or -o path).

The e2e test suite reads this file to call POST /api/v1/ota/my/updates/consent
through the real API Gateway instead of invoking the Lambda directly.

Run this once before the test suite:
  python3 infrastructure/get_pkce_token.py

Usage:
  python3 infrastructure/get_pkce_token.py
  python3 infrastructure/get_pkce_token.py -u USER -p PASS -o /tmp/token.txt
"""

import argparse
import base64
import hashlib
import os
import re
import sys
import urllib.parse

try:
    import requests
except ImportError:
    sys.exit("requests not installed — run: pip install requests")

COGNITO_DOMAIN = "https://ap-south-1h1o8s7257.auth.ap-south-1.amazoncognito.com"
CLIENT_ID      = "q7189jitfkk4ttesepkgls491"
REDIRECT_URI   = "https://oauth.pstmn.io/v1/callback"
SCOPES         = "openid email smarthome_server/read smarthome_server/write"
DEFAULT_OUTPUT = "/tmp/ota_pkce_token.txt"
DEFAULT_USER   = "demotesthw5@yopmail.com"
DEFAULT_PASS   = "DigiluxTest@9900"


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _pkce_pair():
    verifier  = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _extract_csrf(html: str, page_label: str) -> str:
    """
    Return the CSRF hidden-field value from a Cognito Managed Login HTML page.
    Cognito uses name="csrf" (no underscore) in the Managed Login / Remix UI.
    """
    for pattern in (
        r'name="csrf"\s+value="([^"]+)"',
        r'value="([^"]+)"\s+name="csrf"',
        r'name="_csrf"\s+value="([^"]+)"',    # legacy Hosted UI v2 fallback
        r'value="([^"]+)"\s+name="_csrf"',
    ):
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    raise RuntimeError(f"CSRF token not found on {page_label} page")


def _get_form_action(html: str) -> str:
    """Return the <form> action URL, resolving HTML entities."""
    m = re.search(r'<form[^>]+action="([^"]+)"', html)
    if not m:
        raise RuntimeError("No <form action=...> found on page")
    return m.group(1).replace("&amp;", "&")


def _resolve(location: str) -> str:
    """Resolve a possibly-relative redirect Location to an absolute URL."""
    if location.startswith("http"):
        return location
    return urllib.parse.urljoin(COGNITO_DOMAIN, location)


def _follow_to_callback(session: requests.Session,
                        resp: requests.Response) -> str:
    """
    Follow redirects from *resp* manually until we hit the callback URL
    (the one containing ?code=).  Returns the full callback URL.
    Never makes a request to the callback host itself.
    """
    for _ in range(12):
        if resp.status_code not in (301, 302, 303, 307, 308):
            raise RuntimeError(
                f"Expected redirect but got HTTP {resp.status_code}: {resp.text[:300]}"
            )
        location = _resolve(resp.headers.get("Location", ""))
        if not location:
            raise RuntimeError("Redirect with no Location header")

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)

        if "code" in qs or location.startswith(REDIRECT_URI):
            return location

        if "error" in qs:
            desc = qs.get("error_description", qs.get("error", ["unknown"]))[0]
            raise RuntimeError(f"Cognito error in redirect: {desc}")

        resp = session.get(location, allow_redirects=False)

    raise RuntimeError("Too many redirects without reaching callback URL")


# ── Main PKCE flow ────────────────────────────────────────────────────────────

def get_token(username: str, password: str) -> str:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 DigiluxE2ETest/1.0"})

    verifier, challenge = _pkce_pair()

    # Step 1 — GET /oauth2/authorize → auto-follow to the login page
    auth_url = (
        f"{COGNITO_DOMAIN}/oauth2/authorize"
        f"?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
        f"&scope={urllib.parse.quote(SCOPES, safe='')}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
    )
    resp = session.get(auth_url, allow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"/oauth2/authorize returned HTTP {resp.status_code}")

    csrf1      = _extract_csrf(resp.text, "login")
    login_url  = _resolve(_get_form_action(resp.text))

    # Step 2 — POST username → auto-follow to /verifyPassword
    resp2 = session.post(
        login_url,
        data={"csrf": csrf1, "username": username, "cognitoAsfData": ""},
        allow_redirects=True,
    )
    if resp2.status_code != 200:
        raise RuntimeError(f"/login POST returned HTTP {resp2.status_code}")

    csrf2       = _extract_csrf(resp2.text, "verifyPassword")
    verify_url  = _resolve(_get_form_action(resp2.text))

    # Step 3 — POST password — do NOT auto-follow (callback redirect is next)
    resp3 = session.post(
        verify_url,
        data={"csrf": csrf2, "password": password, "cognitoAsfData": ""},
        allow_redirects=False,
    )

    callback_url = _follow_to_callback(session, resp3)

    # Step 4 — Extract auth code from callback URL
    qs   = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
    code = qs.get("code", [None])[0]
    if not code:
        desc = qs.get("error_description", qs.get("error", ["no code in callback"]))[0]
        raise RuntimeError(f"Auth code not in callback: {desc}")

    # Step 5 — Exchange code + verifier for tokens
    token_resp = requests.post(
        f"{COGNITO_DOMAIN}/oauth2/token",
        data={
            "grant_type":    "authorization_code",
            "client_id":     CLIENT_ID,
            "code":          code,
            "redirect_uri":  REDIRECT_URI,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token_resp.raise_for_status()
    j = token_resp.json()
    if "access_token" not in j:
        raise RuntimeError(f"Token exchange failed: {j}")
    return j["access_token"]


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-u", "--username", default=DEFAULT_USER,
                        help=f"Cognito username (default: {DEFAULT_USER})")
    parser.add_argument("-p", "--password", default=DEFAULT_PASS,
                        help="Cognito password")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT,
                        help=f"Output file for the token (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args()

    print(f"Obtaining PKCE token for {args.username} …", flush=True)
    try:
        token = get_token(args.username, args.password)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    with open(args.output, "w") as fh:
        fh.write(token + "\n")

    preview = f"{token[:8]}…{token[-8:]}"
    print(f"Token saved → {args.output}  ({preview})")


if __name__ == "__main__":
    main()
