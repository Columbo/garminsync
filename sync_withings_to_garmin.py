import datetime as dt
import io
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import garth
import requests
from garth.exc import GarthHTTPError
from requests import HTTPError

try:
    from garminconnect.fit import FitEncoderWeight
except ImportError:
    FitEncoderWeight = None  # type: ignore[assignment]


WITHINGS_OAUTH_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
WITHINGS_MEASURE_URL = "https://wbsapi.withings.net/measure"


@dataclass(frozen=True)
class WeightEntry:
    timestamp_local: dt.datetime
    kilograms: float
    body_fat_percent: float | None = None
    muscle_kg: float | None = None
    bone_kg: float | None = None
    hydration_percent: float | None = None
    fat_mass_kg: float | None = None
    bmi: float | None = None


# Withings measure type ids used for body composition.
WITHINGS_TYPE_WEIGHT = 1
WITHINGS_TYPE_FAT_PERCENT = 6
WITHINGS_TYPE_FAT_MASS = 8
WITHINGS_TYPE_HYDRATION_PERCENT = 77
WITHINGS_TYPE_MUSCLE_MASS = 76
WITHINGS_TYPE_BONE_MASS = 88
WITHINGS_TYPE_BMI = 11


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _export_github_env(name: str, value: str) -> None:
    github_env = os.getenv("GITHUB_ENV")
    if not github_env:
        return

    with open(github_env, "a", encoding="utf-8") as env_file:
        env_file.write(f"{name}<<EOF\n{value}\nEOF\n")


def refresh_withings_access_token(
    client_id: str, client_secret: str, refresh_token: str
) -> dict:
    payload = {
        "action": "requesttoken",
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    response = requests.post(WITHINGS_OAUTH_TOKEN_URL, data=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("status") != 0:
        raise RuntimeError(f"Withings token refresh failed: {data}")
    return data["body"]


def fetch_withings_weight_entries(access_token: str, start_date: dt.datetime) -> list[WeightEntry]:
    params = {
        "action": "getmeas",
        "category": 1,  # real measurements
        "startdate": int(start_date.timestamp()),
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(WITHINGS_MEASURE_URL, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("status") != 0:
        raise RuntimeError(f"Withings getmeas failed: {data}")

    entries: list[WeightEntry] = []
    for group in data.get("body", {}).get("measuregrps", []):
        raw_ts = int(group["date"])
        group_tz_name = group.get("timezone")
        ts_local: dt.datetime
        if group_tz_name:
            try:
                group_tz = ZoneInfo(group_tz_name)
                # Withings `date` is treated as local wall-clock seconds.
                ts_local = dt.datetime.fromtimestamp(raw_ts, dt.UTC).replace(tzinfo=group_tz)
            except ZoneInfoNotFoundError:
                local_tz = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
                ts_local = dt.datetime.fromtimestamp(raw_ts, dt.UTC).replace(tzinfo=local_tz)
        else:
            local_tz = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
            ts_local = dt.datetime.fromtimestamp(raw_ts, dt.UTC).replace(tzinfo=local_tz)

        parsed: dict[int, float] = {}
        for measure in group.get("measures", []):
            measure_type = measure.get("type")
            if measure_type is None:
                continue
            parsed[measure_type] = measure["value"] * (10 ** measure["unit"])

        if WITHINGS_TYPE_WEIGHT not in parsed:
            continue

        entries.append(
            WeightEntry(
                timestamp_local=ts_local,
                kilograms=parsed[WITHINGS_TYPE_WEIGHT],
                body_fat_percent=parsed.get(WITHINGS_TYPE_FAT_PERCENT),
                muscle_kg=parsed.get(WITHINGS_TYPE_MUSCLE_MASS),
                bone_kg=parsed.get(WITHINGS_TYPE_BONE_MASS),
                hydration_percent=parsed.get(WITHINGS_TYPE_HYDRATION_PERCENT),
                fat_mass_kg=parsed.get(WITHINGS_TYPE_FAT_MASS),
                bmi=parsed.get(WITHINGS_TYPE_BMI),
            )
        )
    entries.sort(key=lambda entry: entry.timestamp_local)
    return entries


def _iter_unique_by_timestamp(entries: Iterable[WeightEntry]) -> Iterable[WeightEntry]:
    seen: set[tuple[int, int]] = set()
    for entry in entries:
        key = (int(entry.timestamp_local.timestamp()), int(entry.kilograms * 1000))
        if key in seen:
            continue
        seen.add(key)
        yield entry


def _iter_first_entry_per_local_day(entries: Iterable[WeightEntry]) -> Iterable[WeightEntry]:
    first_any_by_day: dict[dt.date, WeightEntry] = {}
    first_with_composition_by_day: dict[dt.date, WeightEntry] = {}
    day_order: list[dt.date] = []

    def has_body_composition(entry: WeightEntry) -> bool:
        return any(
            value is not None
            for value in (
                entry.body_fat_percent,
                entry.muscle_kg,
                entry.bone_kg,
                entry.hydration_percent,
                entry.fat_mass_kg,
                entry.bmi,
            )
        )

    for entry in entries:
        day = entry.timestamp_local.date()
        if day not in first_any_by_day:
            first_any_by_day[day] = entry
            day_order.append(day)
        if day not in first_with_composition_by_day and has_body_composition(entry):
            first_with_composition_by_day[day] = entry

    for day in day_order:
        yield first_with_composition_by_day.get(day, first_any_by_day[day])


def upload_weight_to_garmin(entry: WeightEntry) -> None:
    if FitEncoderWeight is not None:
        upload_body_composition_fit_to_garmin(entry)
        return

    local_timestamp = entry.timestamp_local
    gmt_timestamp = local_timestamp.astimezone(dt.timezone.utc)

    def _fmt_ts(value: dt.datetime) -> str:
        # Garmin expects millisecond precision and no timezone suffix.
        return value.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]

    payload = {
        "dateTimestamp": _fmt_ts(local_timestamp),
        "gmtTimestamp": _fmt_ts(gmt_timestamp),
        "unitKey": "kg",
        "value": round(entry.kilograms, 3),
        "sourceType": "MANUAL",
    }
    # garth's HTTP client expects subdomain+path for low-level methods.
    # Use connectapi helper, which sets auth and target host correctly.
    garth.client.connectapi("/weight-service/user-weight", method="POST", json=payload)


def upload_body_composition_fit_to_garmin(entry: WeightEntry) -> None:
    if FitEncoderWeight is None:
        raise RuntimeError("garminconnect FIT encoder is not available")

    encoder = FitEncoderWeight()
    encoder.write_file_info()
    encoder.write_file_creator()
    encoder.write_device_info(entry.timestamp_local)
    encoder.write_weight_scale(
        entry.timestamp_local,
        weight=round(entry.kilograms, 3),
        percent_fat=entry.body_fat_percent,
        percent_hydration=entry.hydration_percent,
        bone_mass=entry.bone_kg,
        muscle_mass=entry.muscle_kg,
        bmi=entry.bmi,
        visceral_fat_mass=entry.fat_mass_kg,
    )
    encoder.finish()

    fit_bytes = encoder.getvalue()
    file_obj = io.BytesIO(fit_bytes)
    file_obj.name = "body_composition.fit"  # used by garth.client.upload
    result = garth.client.upload(file_obj)
    if isinstance(result, dict) and result.get("detailedImportResult", {}).get("failures"):
        raise RuntimeError(f"Garmin FIT import reported failures: {result['detailedImportResult']['failures']}")


def _resume_garmin_session_from_env() -> bool:
    oauth1 = os.getenv("GARTH_OAUTH1_TOKEN_JSON")
    oauth2 = os.getenv("GARTH_OAUTH2_TOKEN_JSON")
    if not oauth1 or not oauth2:
        return False

    with tempfile.TemporaryDirectory(prefix="garth-session-") as temp_dir:
        session_dir = Path(temp_dir)
        (session_dir / "oauth1_token.json").write_text(oauth1, encoding="utf-8")
        (session_dir / "oauth2_token.json").write_text(oauth2, encoding="utf-8")
        garth.resume(str(session_dir))
    print("Garmin session resumed from GARTH_OAUTH*_TOKEN_JSON secrets.")
    return True


def login_garmin() -> None:
    if _resume_garmin_session_from_env():
        return

    garmin_email = _required_env("GARMIN_EMAIL")
    garmin_password = _required_env("GARMIN_PASSWORD")
    garth.login(garmin_email, garmin_password)
    print(
        "Garmin login used username/password. "
        "For unattended runs, set GARTH_OAUTH1_TOKEN_JSON and GARTH_OAUTH2_TOKEN_JSON."
    )


def main() -> int:
    withings_client_id = _required_env("WITHINGS_CLIENT_ID")
    withings_client_secret = _required_env("WITHINGS_CLIENT_SECRET")
    withings_refresh_token = _required_env("WITHINGS_REFRESH_TOKEN")
    withings_access_token = _required_env("WITHINGS_ACCESS_TOKEN")
    lookback_days = int(os.getenv("LOOKBACK_DAYS", "7"))

    token_body = refresh_withings_access_token(
        client_id=withings_client_id,
        client_secret=withings_client_secret,
        refresh_token=withings_refresh_token,
    )
    refreshed_access_token = token_body.get("access_token", withings_access_token)
    refreshed_refresh_token = token_body.get("refresh_token", withings_refresh_token)

    if refreshed_access_token != withings_access_token:
        print("Withings access token was refreshed.")
        _export_github_env("WITHINGS_ACCESS_TOKEN_UPDATED", "true")
        _export_github_env("WITHINGS_ACCESS_TOKEN_NEXT", refreshed_access_token)
    if refreshed_refresh_token != withings_refresh_token:
        print("Withings refresh token was refreshed. Update your GitHub secret WITHINGS_REFRESH_TOKEN.")
        _export_github_env("WITHINGS_REFRESH_TOKEN_UPDATED", "true")
        _export_github_env("WITHINGS_REFRESH_TOKEN_NEXT", refreshed_refresh_token)

    start_date = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=lookback_days)
    entries = list(_iter_unique_by_timestamp(fetch_withings_weight_entries(refreshed_access_token, start_date)))
    entries = list(_iter_first_entry_per_local_day(entries))
    if not entries:
        print("No Withings weight entries found.")
        return 0

    print(f"Found {len(entries)} weight entries from Withings.")
    if FitEncoderWeight is None:
        print("garminconnect not installed: using weight-only JSON upload.")
    else:
        print("Using FIT upload path (weight + available body composition metrics).")
    login_garmin()
    uploaded = 0
    skipped = 0
    for entry in entries:
        try:
            upload_weight_to_garmin(entry)
            uploaded += 1
            print(f"Uploaded: {entry.timestamp_local.isoformat()} -> {entry.kilograms:.3f} kg")
        except (HTTPError, GarthHTTPError) as exc:
            status_code = None
            if isinstance(exc, GarthHTTPError) and exc.error.response is not None:
                status_code = exc.error.response.status_code
            elif isinstance(exc, requests.HTTPError) and exc.response is not None:
                status_code = exc.response.status_code

            # Garmin may reject duplicate entries with 4xx. Keep sync idempotent.
            if status_code in {400, 409}:
                skipped += 1
                print(f"Skipped duplicate/conflict: {entry.timestamp_local.isoformat()} ({entry.kilograms:.3f} kg)")
                continue
            raise

    print(f"Sync complete. Uploaded={uploaded}, Skipped={skipped}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise
