import argparse
import json
import os
import sys

import requests


TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"


def _arg_or_env(value: str | None, env_name: str) -> str:
    if value:
        return value
    env_value = os.getenv(env_name)
    if env_value:
        return env_value
    raise RuntimeError(f"Missing required value: --{env_name.lower()} or {env_name}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exchange Withings authorization code for access/refresh tokens."
    )
    parser.add_argument("--client-id", help="Withings client id")
    parser.add_argument("--client-secret", help="Withings client secret")
    parser.add_argument("--code", required=True, help="Authorization code from redirect URL")
    parser.add_argument(
        "--redirect-uri",
        default=os.getenv("WITHINGS_REDIRECT_URI", "http://localhost/callback"),
        help="Redirect URI configured in your Withings app",
    )
    args = parser.parse_args()

    client_id = _arg_or_env(args.client_id, "WITHINGS_CLIENT_ID")
    client_secret = _arg_or_env(args.client_secret, "WITHINGS_CLIENT_SECRET")

    payload = {
        "action": "requesttoken",
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": args.code,
        "redirect_uri": args.redirect_uri,
    }
    response = requests.post(TOKEN_URL, data=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("status") != 0:
        raise RuntimeError(f"Withings token exchange failed: {json.dumps(data)}")

    body = data["body"]
    print("WITHINGS_ACCESS_TOKEN=" + body["access_token"])
    print("WITHINGS_REFRESH_TOKEN=" + body["refresh_token"])
    print("WITHINGS_EXPIRES_IN=" + str(body.get("expires_in", "")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise
