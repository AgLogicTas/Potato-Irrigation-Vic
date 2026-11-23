#!/usr/bin/env python3
"""
Fetches a 7-day ETo / rainfall forecast for each Ag Logic potato region from
Open-Meteo (free, no API key needed) and writes data.json.

Run automatically by .github/workflows/update-weather.yml. You can also run it
yourself with: python3 fetch_weather.py

How it works (changed Nov 2026 after Dean/Newlyn went stale):
- ALL locations are requested in ONE call to Open-Meteo (instead of one call
  per location), so there is only one connection that can time out.
- That call is retried up to 6 times with growing pauses and a long timeout.
- If the one big call still fails, it falls back to one call per location.
- If a location still can't be fetched, its previous forecast is kept in
  data.json (so the Power Automate flow never sees null) BUT the script then
  exits with an error, so the GitHub run turns RED instead of looking fine.
  The data.json file is still saved/committed first.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

# Real-world coordinates for every location offered on the calculator.
REGIONS = [
    {"id": "vic-dean", "name": "Dean", "lat": -37.46667, "lon": 143.98333, "tz": "Australia/Melbourne"},
    {"id": "vic-newlyn", "name": "Newlyn", "lat": -37.41700, "lon": 143.98100, "tz": "Australia/Melbourne"},
    {"id": "vic-blampied", "name": "Blampied", "lat": -37.36667, "lon": 144.05000, "tz": "Australia/Melbourne"},
    {"id": "vic-glenlyon", "name": "Glenlyon", "lat": -37.25000, "lon": 144.25000, "tz": "Australia/Melbourne"},
    {"id": "vic-pootilla", "name": "Pootilla", "lat": -37.52400, "lon": 143.96700, "tz": "Australia/Melbourne"},
    {"id": "vic-bungaree", "name": "Bungaree", "lat": -37.55722, "lon": 144.00444, "tz": "Australia/Melbourne"},
    {"id": "vic-dunnstown", "name": "Dunnstown", "lat": -37.60000, "lon": 143.98333, "tz": "Australia/Melbourne"},
    {"id": "vic-bolwarrah", "name": "Bolwarrah", "lat": -37.51667, "lon": 144.10000, "tz": "Australia/Melbourne"},
    {"id": "vic-windermere", "name": "Windermere", "lat": -37.51167, "lon": 143.70611, "tz": "Australia/Melbourne"},
    {"id": "vic-tourello", "name": "Tourello", "lat": -37.35340, "lon": 143.77660, "tz": "Australia/Melbourne"},
]

API_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 60              # seconds to wait for Open-Meteo (was 30)
BATCH_ATTEMPTS = 6        # tries for the one-call-for-everything request
SINGLE_ATTEMPTS = 4       # tries per location in the fallback
PAUSE_BETWEEN_SINGLES = 2 # seconds
DAILY = "et0_fao_evapotranspiration,precipitation_sum"


def call_api(lats, lons, tz):
    params = {
        "latitude": ",".join(str(x) for x in lats),
        "longitude": ",".join(str(x) for x in lons),
        "daily": DAILY,
        "timezone": tz,
        "forecast_days": 7,
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        payload = json.load(resp)
    # One location returns an object; several return a list (same order).
    return payload if isinstance(payload, list) else [payload]


def to_week(item):
    daily = item["daily"]
    dates = daily["time"]
    eto = daily["et0_fao_evapotranspiration"]
    rain = daily["precipitation_sum"]
    week = [{
        "date": dates[i],
        "eto": round(eto[i], 2) if eto[i] is not None else None,
        "rain": round(rain[i], 2) if rain[i] is not None else None,
    } for i in range(len(dates))]
    # A week with any missing value counts as a failure so it gets retried.
    if len(week) < 7 or any(d["eto"] is None or d["rain"] is None for d in week):
        raise ValueError("Open-Meteo returned incomplete data")
    return week


def with_retries(fn, attempts, label):
    last = None
    for n in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last = exc
            print(f"  attempt {n}/{attempts} failed for {label}: {exc}")
            if n < attempts:
                time.sleep(5 * n)  # 5s, 10s, 15s ...
    raise last


def load_previous():
    if not os.path.exists("data.json"):
        return {}
    try:
        with open("data.json") as f:
            return json.load(f).get("regions", {}) or {}
    except Exception:
        return {}


def main():
    previous = load_previous()
    regions_out = {}
    failed = []

    # Group by timezone so each request has one timezone.
    groups = {}
    for r in REGIONS:
        groups.setdefault(r.get("tz", "Australia/Hobart"), []).append(r)

    for tz, regs in groups.items():
        print(f"Fetching {len(regs)} locations in one request ({tz})...")
        batch = None
        try:
            batch = with_retries(
                lambda: call_api([r["lat"] for r in regs], [r["lon"] for r in regs], tz),
                BATCH_ATTEMPTS, "the batch request")
            if len(batch) != len(regs):
                raise ValueError(f"expected {len(regs)} locations, got {len(batch)}")
        except Exception as exc:
            print(f"  batch request failed ({exc}); falling back to one request per location")
            batch = None

        for i, r in enumerate(regs):
            week = None
            if batch is not None:
                try:
                    week = to_week(batch[i])
                except Exception as exc:
                    print(f"  {r['name']}: bad data in batch ({exc})")
            if week is None:
                print(f"Fetching {r['name']} on its own...")
                try:
                    week = with_retries(
                        lambda: to_week(call_api([r["lat"]], [r["lon"]], tz)[0]),
                        SINGLE_ATTEMPTS, r["name"])
                except Exception as exc:
                    print(f"  FAILED for {r['name']}: {exc}")
                    failed.append(r["name"])
                    week = previous.get(r["id"])  # keep yesterday's, never null-out
                time.sleep(PAUSE_BETWEEN_SINGLES)
            regions_out[r["id"]] = week

    first_tz = REGIONS[0].get("tz", "Australia/Hobart")
    out = {
        "generated_date": datetime.now(ZoneInfo(first_tz)).strftime("%Y-%m-%d"),
        "regions": regions_out,
    }
    with open("data.json", "w") as f:
        json.dump(out, f, indent=2)
    print("Wrote data.json")

    if failed:
        print("ERROR: could not refresh (old forecast kept): " + ", ".join(failed))
        sys.exit(1)  # makes the GitHub run go RED so it can't fail quietly
    print("All locations refreshed.")


if __name__ == "__main__":
    main()
