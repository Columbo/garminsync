import json
import os
from pathlib import Path

import garth


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    email = _required_env("GARMIN_EMAIL")
    password = _required_env("GARMIN_PASSWORD")
    out_dir = Path(os.getenv("GARTH_SESSION_DIR", ".garth"))

    print("Logging into Garmin. If MFA is enabled, enter the code when prompted.")
    garth.login(email, password)
    garth.save(str(out_dir))
    print(f"Saved Garmin session files to {out_dir.resolve()}")

    oauth1 = (out_dir / "oauth1_token.json").read_text(encoding="utf-8")
    oauth2 = (out_dir / "oauth2_token.json").read_text(encoding="utf-8")

    # Validate token JSON before printing.
    json.loads(oauth1)
    json.loads(oauth2)

    print("\nCreate these GitHub repository secrets with exact values:")
    print("GARTH_OAUTH1_TOKEN_JSON=" + oauth1)
    print("GARTH_OAUTH2_TOKEN_JSON=" + oauth2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
