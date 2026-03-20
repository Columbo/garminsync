#!/usr/bin/env python3
"""Bootstrap Withings OAuth tokens from client credentials."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.parse

import requests


AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
DEFAULT_SCOPE = "user.info,user.metrics,user.activity"


def _arg_or_env(value: str | None, env_name: str) -> str:
    if value:
        return value
    env_value = os.getenv(env_name)
    if env_value:
        return env_value
    raise RuntimeError(f"Missing required value: --{env_name.lower().replace('_', '-')} or {env_name}")


def _build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "scope": scope,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _exchange_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict:
    response = requests.post(
        TOKEN_URL,
        data={
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status") != 0:
        raise RuntimeError(f"Withings token exchange failed: {json.dumps(data)}")
    return data["body"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Guide the Withings OAuth authorization-code flow and print fresh "
            "WITHINGS_ACCESS_TOKEN and WITHINGS_REFRESH_TOKEN values."
        )
    )
    parser.add_argument("--client-id", help="Withings client id")
    parser.add_argument("--client-secret", help="Withings client secret")
    parser.add_argument(
        "--redirect-uri",
        default=os.getenv("WITHINGS_REDIRECT_URI", "https://www.google.de/"),
        help="Redirect URI configured in your Withings app",
    )
    parser.add_argument(
        "--scope",
        default=os.getenv("WITHINGS_SCOPE", DEFAULT_SCOPE),
        help="OAuth scope list",
    )
    parser.add_argument(
        "--state",
        default=os.getenv("WITHINGS_STATE") or secrets.token_urlsafe(16),
        help="OAuth state value to embed in the authorize URL",
    )
    parser.add_argument(
        "--code",
        help="Authorization code from the redirect URL. If omitted, the script prompts for it.",
    )
    parser.add_argument(
        "--print-url-only",
        action="store_true",
        help="Only print the authorization URL and exit.",
    )
    args = parser.parse_args()

    client_id = _arg_or_env(args.client_id, "WITHINGS_CLIENT_ID")
    client_secret = _arg_or_env(args.client_secret, "WITHINGS_CLIENT_SECRET")

    authorize_url = _build_authorize_url(
        client_id=client_id,
        redirect_uri=args.redirect_uri,
        scope=args.scope,
        state=args.state,
    )

    print("Open this URL in your browser and authorize the app:\n")
    print(authorize_url)
    print()
    print(f"Expected redirect URI: {args.redirect_uri}")
    print(f"State: {args.state}")

    if args.print_url_only:
        return 0

    code = args.code
    if not code:
        print()
        print("After the browser redirects, copy the `code` query parameter from the final URL.")
        code = input("Withings authorization code: ").strip()
    if not code:
        raise RuntimeError("Authorization code is required.")

    token_body = _exchange_code(
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=args.redirect_uri,
    )

    print("\nCreate or update these values:\n")
    print("WITHINGS_ACCESS_TOKEN=" + token_body["access_token"])
    print("WITHINGS_REFRESH_TOKEN=" + token_body["refresh_token"])
    print("WITHINGS_EXPIRES_IN=" + str(token_body.get("expires_in", "")))
    print("WITHINGS_SCOPE=" + str(token_body.get("scope", "")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise
