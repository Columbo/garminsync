#!/usr/bin/env python3
"""
Get Garmin OAuth tokens via real browser login (Playwright).
Bypasses the 429-blocked SSO programmatic login endpoint.

Usage:
  uv run --with playwright --with requests-oauthlib scripts/garmin-browser-auth.py

First time setup (installs Chromium):
  uv run --with playwright python -m playwright install chromium
"""

import base64
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs

import requests
from requests_oauthlib import OAuth1Session
from playwright.sync_api import sync_playwright


OAUTH_CONSUMER_URL = "https://thegarth.s3.amazonaws.com/oauth_consumer.json"
ANDROID_UA = "com.garmin.android.apps.connectmobile"


def get_oauth_consumer():
    """Fetch the shared OAuth consumer key/secret from garth's S3 bucket."""
    resp = requests.get(OAUTH_CONSUMER_URL, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_oauth1_token(ticket: str, consumer: dict) -> dict:
    """Exchange an SSO ticket for an OAuth1 token."""
    sess = OAuth1Session(
        consumer["consumer_key"],
        consumer["consumer_secret"],
    )
    url = (
        f"https://connectapi.garmin.com/oauth-service/oauth/"
        f"preauthorized?ticket={ticket}"
        f"&login-url=https://sso.garmin.com/sso/embed"
        f"&accepts-mfa-tokens=true"
    )
    resp = sess.get(url, headers={"User-Agent": ANDROID_UA}, timeout=15)
    resp.raise_for_status()
    parsed = parse_qs(resp.text)
    token = {k: v[0] for k, v in parsed.items()}
    token["domain"] = "garmin.com"
    return token


def exchange_oauth2(oauth1: dict, consumer: dict) -> dict:
    """Exchange OAuth1 token for OAuth2 token."""
    sess = OAuth1Session(
        consumer["consumer_key"],
        consumer["consumer_secret"],
        resource_owner_key=oauth1["oauth_token"],
        resource_owner_secret=oauth1["oauth_token_secret"],
    )
    url = "https://connectapi.garmin.com/oauth-service/oauth/exchange/user/2.0"
    data = {}
    if oauth1.get("mfa_token"):
        data["mfa_token"] = oauth1["mfa_token"]
    resp = sess.post(
        url,
        headers={
            "User-Agent": ANDROID_UA,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=data,
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json()
    token["expires_at"] = int(time.time() + token["expires_in"])
    token["refresh_token_expires_at"] = int(
        time.time() + token["refresh_token_expires_in"]
    )
    return token


def browser_login() -> str:
    """Open a real browser, let user log in, capture the SSO ticket."""
    ticket = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        # Navigate to SSO embed — same flow the Garmin web app uses
        sso_url = (
            "https://sso.garmin.com/sso/embed"
            "?id=gauth-widget"
            "&embedWidget=true"
            "&gauthHost=https://sso.garmin.com/sso"
            "&clientId=GarminConnect"
            "&locale=en_US"
            "&redirectAfterAccountLoginUrl=https://sso.garmin.com/sso/embed"
            "&service=https://sso.garmin.com/sso/embed"
        )
        page.goto(sso_url)

        print()
        print("=" * 50)
        print("  Browser opened — log in with your Garmin")
        print("  credentials. The window will close")
        print("  automatically when done.")
        print("=" * 50)
        print()

        # Wait for the success redirect that contains the ticket
        # The SSO flow ends with a page containing 'ticket=ST-...'
        max_wait = 300  # 5 minutes
        start = time.time()
        while time.time() - start < max_wait:
            try:
                content = page.content()
                # Look for ticket in page content (ST- prefix)
                m = re.search(r'ticket=(ST-[A-Za-z0-9\-]+)', content)
                if m:
                    ticket = m.group(1)
                    print(f"Got ticket: {ticket[:30]}...")
                    break

                # Also check URL for ticket param
                url = page.url
                if "ticket=" in url:
                    m = re.search(r'ticket=(ST-[A-Za-z0-9\-]+)', url)
                    if m:
                        ticket = m.group(1)
                        print(f"Got ticket from URL: {ticket[:30]}...")
                        break
            except Exception:
                pass

            page.wait_for_timeout(500)

        browser.close()

    if not ticket:
        print("ERROR: Timed out waiting for login (5 min). Try again.")
        raise SystemExit(1)

    return ticket


def main():
    print("Garmin Browser Auth")
    print("=" * 50)

    # Step 1: Get OAuth consumer credentials
    print("Fetching OAuth consumer credentials...")
    consumer = get_oauth_consumer()

    # Step 2: Browser login to get SSO ticket
    print("Launching browser...")
    ticket = browser_login()

    # Step 3: Exchange ticket for OAuth1
    print("Exchanging ticket for OAuth1 token...")
    oauth1 = get_oauth1_token(ticket, consumer)
    print(f"  OAuth1 token: {oauth1['oauth_token'][:20]}...")

    # Step 4: Exchange OAuth1 for OAuth2
    print("Exchanging OAuth1 for OAuth2 token...")
    oauth2 = exchange_oauth2(oauth1, consumer)
    print(f"  OAuth2 access_token: {oauth2['access_token'][:20]}...")
    print(f"  Expires in: {oauth2['expires_in']}s")
    print(f"  Refresh expires in: {oauth2['refresh_token_expires_in']}s")

    # Step 5: Verify tokens work
    print("Verifying tokens...")
    verify_resp = requests.get(
        "https://connectapi.garmin.com/userprofile-service/socialProfile",
        headers={
            "User-Agent": "GCM-iOS-5.7.2.1",
            "Authorization": f"Bearer {oauth2['access_token']}",
        },
        timeout=15,
    )
    verify_resp.raise_for_status()
    profile = verify_resp.json()
    print(f"  Authenticated as: {profile.get('displayName', 'unknown')}")

    # Step 6: Save tokens
    garth_dir = Path.home() / ".garth"
    garth_dir.mkdir(exist_ok=True)
    (garth_dir / "oauth1_token.json").write_text(json.dumps(oauth1, indent=2))
    (garth_dir / "oauth2_token.json").write_text(json.dumps(oauth2, indent=2))
    print(f"\nTokens saved to {garth_dir}")

    # Step 7: Output base64 bundle for GitHub secret
    bundle = {"oauth1": oauth1, "oauth2": oauth2}
    b64 = base64.b64encode(json.dumps(bundle).encode()).decode()

    print("\n" + "=" * 50)
    print("GARMIN_TOKEN_B64 (paste into GitHub secret):")
    print("=" * 50)
    print(b64)
    print("=" * 50)


if __name__ == "__main__":
    main()