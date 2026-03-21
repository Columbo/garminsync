#!/usr/bin/env python3
"""
Bootstrap Garmin session secrets via a real browser login using Playwright.

This is a manual fallback for the initial Garmin bootstrap when the regular
garth username/password login hits Garmin's rate limits or other SSO blocking.

Dependencies for this script are intentionally optional so the scheduled sync
workflow does not need Playwright installed:

  pip install playwright requests requests-oauthlib garth
  python -m playwright install chromium
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs


OAUTH_CONSUMER_URL = "https://thegarth.s3.amazonaws.com/oauth_consumer.json"
ANDROID_UA = "com.garmin.android.apps.connectmobile"
SOCIAL_PROFILE_URL = "https://connectapi.garmin.com/userprofile-service/socialProfile"
SSO_EMBED_URL = (
    "https://sso.garmin.com/sso/embed"
    "?id=gauth-widget"
    "&embedWidget=true"
    "&gauthHost=https://sso.garmin.com/sso"
    "&clientId=GarminConnect"
    "&locale=en_US"
    "&redirectAfterAccountLoginUrl=https://sso.garmin.com/sso/embed"
    "&service=https://sso.garmin.com/sso/embed"
)


def _import_runtime_deps():
    try:
        import requests
        from playwright.sync_api import sync_playwright
        from requests_oauthlib import OAuth1Session
    except ImportError as exc:
        missing = exc.name or "required dependency"
        raise SystemExit(
            "Missing dependency for browser bootstrap: "
            f"{missing}. Install with:\n"
            "  pip install playwright requests requests-oauthlib garth\n"
            "  python -m playwright install chromium"
        ) from exc

    return requests, sync_playwright, OAuth1Session


def _load_garth():
    try:
        os.environ.setdefault("GARTH_TELEMETRY_ENABLED", "false")
        os.environ.setdefault("GARTH_TELEMETRY_SEND_TO_LOGFIRE", "false")
        import garth
    except ImportError:
        return None

    return garth


def get_oauth_consumer(requests):
    resp = requests.get(OAUTH_CONSUMER_URL, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_oauth1_token(ticket: str, consumer: dict, OAuth1Session) -> dict:
    sess = OAuth1Session(
        consumer["consumer_key"],
        consumer["consumer_secret"],
    )
    url = (
        "https://connectapi.garmin.com/oauth-service/oauth/"
        f"preauthorized?ticket={ticket}"
        "&login-url=https://sso.garmin.com/sso/embed"
        "&accepts-mfa-tokens=true"
    )
    resp = sess.get(url, headers={"User-Agent": ANDROID_UA}, timeout=15)
    resp.raise_for_status()
    parsed = parse_qs(resp.text)
    token = {key: values[0] for key, values in parsed.items()}
    token["domain"] = "garmin.com"
    return token


def exchange_oauth2(oauth1: dict, consumer: dict, OAuth1Session) -> dict:
    sess = OAuth1Session(
        consumer["consumer_key"],
        consumer["consumer_secret"],
        resource_owner_key=oauth1["oauth_token"],
        resource_owner_secret=oauth1["oauth_token_secret"],
    )
    data = {}
    if oauth1.get("mfa_token"):
        data["mfa_token"] = oauth1["mfa_token"]

    resp = sess.post(
        "https://connectapi.garmin.com/oauth-service/oauth/exchange/user/2.0",
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


def browser_login(sync_playwright) -> str:
    ticket = None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(SSO_EMBED_URL)

        print()
        print("=" * 50)
        print("  Browser opened. Complete the Garmin login flow")
        print("  including MFA if prompted. The window will")
        print("  close automatically after the ticket is captured.")
        print("=" * 50)
        print()

        max_wait_seconds = 300
        started_at = time.time()
        while time.time() - started_at < max_wait_seconds:
            try:
                content = page.content()
                match = re.search(r"ticket=(ST-[A-Za-z0-9-]+)", content)
                if match:
                    ticket = match.group(1)
                    break

                url = page.url
                if "ticket=" in url:
                    match = re.search(r"ticket=(ST-[A-Za-z0-9-]+)", url)
                    if match:
                        ticket = match.group(1)
                        break
            except Exception:
                pass

            page.wait_for_timeout(500)

        browser.close()

    if not ticket:
        raise SystemExit("Timed out waiting for Garmin login to complete.")

    return ticket


def verify_oauth2_with_requests(oauth2: dict, requests) -> dict:
    resp = requests.get(
        SOCIAL_PROFILE_URL,
        headers={
            "User-Agent": "GCM-iOS-5.7.2.1",
            "Authorization": f"Bearer {oauth2['access_token']}",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def maybe_build_garth_token(out_dir: Path) -> tuple[str | None, str | None]:
    garth = _load_garth()
    if garth is None:
        return None, None

    # Ensure garth reads the standard token files we just wrote and validates
    # the session against Connect before emitting GARTH_TOKEN.
    garth.client.oauth1_token = None
    garth.client.oauth2_token = None
    garth.client._user_profile = None
    garth.resume(str(out_dir))
    profile = garth.client.connectapi("/userprofile-service/socialProfile")
    garth.save(str(out_dir))
    garth_token = garth.client.dumps()
    return garth_token, profile.get("displayName")


def main() -> int:
    requests, sync_playwright, OAuth1Session = _import_runtime_deps()
    out_dir = Path(os.getenv("GARTH_SESSION_DIR", ".garth"))
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Garmin Browser Bootstrap")
    print("=" * 50)

    print("Fetching OAuth consumer credentials...")
    consumer = get_oauth_consumer(requests)

    print("Launching browser login...")
    ticket = browser_login(sync_playwright)

    print("Exchanging SSO ticket for OAuth1 token...")
    oauth1 = get_oauth1_token(ticket, consumer, OAuth1Session)

    print("Exchanging OAuth1 token for OAuth2 token...")
    oauth2 = exchange_oauth2(oauth1, consumer, OAuth1Session)

    print("Verifying OAuth2 token...")
    profile = verify_oauth2_with_requests(oauth2, requests)
    display_name = profile.get("displayName", "unknown")
    print(f"Authenticated as: {display_name}")

    oauth1_path = out_dir / "oauth1_token.json"
    oauth2_path = out_dir / "oauth2_token.json"
    oauth1_json = json.dumps(oauth1)
    oauth2_json = json.dumps(oauth2)
    oauth1_path.write_text(oauth1_json, encoding="utf-8")
    oauth2_path.write_text(oauth2_json, encoding="utf-8")
    print(f"Saved Garmin session files to {out_dir.resolve()}")

    garth_token = None
    garth_display_name = None
    garth_warning = None
    try:
        garth_token, garth_display_name = maybe_build_garth_token(out_dir)
    except Exception as exc:
        garth_warning = str(exc)

    if garth_display_name:
        print(f"Validated with garth as: {garth_display_name}")
    elif garth_warning:
        print(f"Skipping GARTH_TOKEN output because garth validation failed: {garth_warning}")

    print("\nCreate these GitHub repository secrets with exact values:")
    if garth_token:
        print("GARTH_TOKEN=" + garth_token)
    elif garth_warning:
        print("# GARTH_TOKEN not emitted because garth validation failed: " + garth_warning)
    else:
        print(
            "# GARTH_TOKEN not emitted because the optional 'garth' package is "
            "not installed in this environment."
        )
    print("GARTH_OAUTH1_TOKEN_JSON=" + oauth1_json)
    print("GARTH_OAUTH2_TOKEN_JSON=" + oauth2_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
