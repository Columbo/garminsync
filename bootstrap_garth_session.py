import base64
import json
import os
import time
from pathlib import Path

# garth 0.7+ enables telemetry by default during import. Keep bootstrap
# output focused on the tokens the user actually needs.
os.environ.setdefault("GARTH_TELEMETRY_ENABLED", "false")
os.environ.setdefault("GARTH_TELEMETRY_SEND_TO_LOGFIRE", "false")

import garth
from garth.exc import GarthHTTPError


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return float(value)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _status_code_from_exception(exc: BaseException) -> int | None:
    if isinstance(exc, GarthHTTPError) and exc.error.response is not None:
        return exc.error.response.status_code
    return None


def _retry_after_seconds(exc: BaseException) -> float | None:
    if not isinstance(exc, GarthHTTPError) or exc.error.response is None:
        return None

    retry_after = exc.error.response.headers.get("Retry-After")
    if retry_after is None:
        return None

    try:
        return max(0.0, float(retry_after))
    except ValueError:
        return None


def _resume_garmin_session_from_env() -> bool:
    garth_token = os.getenv("GARTH_TOKEN")
    if garth_token:
        garth.client.loads(garth_token)
        print("Garmin session resumed from GARTH_TOKEN.")
        return True

    oauth1 = os.getenv("GARTH_OAUTH1_TOKEN_JSON")
    oauth2 = os.getenv("GARTH_OAUTH2_TOKEN_JSON")
    if not oauth1 or not oauth2:
        return False

    out_dir = Path(os.getenv("GARTH_SESSION_DIR", ".garth"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "oauth1_token.json").write_text(oauth1, encoding="utf-8")
    (out_dir / "oauth2_token.json").write_text(oauth2, encoding="utf-8")
    garth.resume(str(out_dir))
    print("Garmin session resumed from GARTH_OAUTH*_TOKEN_JSON.")
    return True


def _login_with_retry(email: str, password: str) -> None:
    attempts = max(1, _env_int("GARMIN_BOOTSTRAP_RETRY_ATTEMPTS", 4))
    base_delay = max(0.0, _env_float("GARMIN_BOOTSTRAP_RETRY_BACKOFF_SECONDS", 20.0))
    max_delay = max(
        base_delay, _env_float("GARMIN_BOOTSTRAP_RETRY_MAX_BACKOFF_SECONDS", 120.0)
    )

    for attempt in range(1, attempts + 1):
        try:
            garth.login(email, password)
            return
        except GarthHTTPError as exc:
            status_code = _status_code_from_exception(exc)
            if status_code != 429 or attempt >= attempts:
                raise

            backoff_seconds = _retry_after_seconds(exc)
            if backoff_seconds is None:
                backoff_seconds = min(max_delay, base_delay * (2 ** (attempt - 1)))

            print(
                f"Garmin login was rate-limited (attempt {attempt}/{attempts}). "
                f"Retrying in {backoff_seconds:.1f}s."
            )
            time.sleep(backoff_seconds)


def main() -> int:
    out_dir = Path(os.getenv("GARTH_SESSION_DIR", ".garth"))

    if not _resume_garmin_session_from_env():
        email = _required_env("GARMIN_EMAIL")
        password = _required_env("GARMIN_PASSWORD")
        print("Logging into Garmin. If MFA is enabled, enter the code when prompted.")
        _login_with_retry(email, password)
    garth.save(str(out_dir))
    print(f"Saved Garmin session files to {out_dir.resolve()}")

    oauth1 = (out_dir / "oauth1_token.json").read_text(encoding="utf-8")
    oauth2 = (out_dir / "oauth2_token.json").read_text(encoding="utf-8")
    garth_token = garth.client.dumps()

    # Validate token JSON before printing.
    json.loads(oauth1)
    json.loads(oauth2)
    json.loads(base64.b64decode(garth_token))

    print("\nCreate these GitHub repository secrets with exact values:")
    print("GARTH_TOKEN=" + garth_token)
    print("GARTH_OAUTH1_TOKEN_JSON=" + oauth1)
    print("GARTH_OAUTH2_TOKEN_JSON=" + oauth2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
