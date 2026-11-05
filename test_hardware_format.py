import os
import sys
import uuid
from datetime import datetime, timezone

import requests

# Usage:  python test_hardware_format.py [LAT LON]
DEFAULT_LAT, DEFAULT_LON = 28.6139, 77.2090

if len(sys.argv) >= 3:
    BASE_LAT, BASE_LON = float(sys.argv[1]), float(sys.argv[2])
else:
    BASE_LAT, BASE_LON = DEFAULT_LAT, DEFAULT_LON

BASE = os.getenv("TT_URL", "http://127.0.0.1:8000")
HEADERS = {"X-API-Key": os.getenv("TT_API_KEY", "")}
run = uuid.uuid4().hex[:6]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make(event_id, **changes):
    r = {"device_id": "TT-TEST", "event_id": f"{run}-{event_id}", "recorded_at": now_iso(),
         "latitude": BASE_LAT + 0.0004, "longitude": BASE_LON + 0.0004, "vibration": 3.1,
         "speed_kmph": 32.0, "satellites": 8, "hdop": 1.2, "event": "possible_pothole"}
    r.update(changes)
    return r


def send(label, payload):
    r = requests.post(f"{BASE}/report", json=payload, headers=HEADERS, timeout=90)
    print(label, "->", r.status_code, r.json())


print(f"Testing {BASE} around {BASE_LAT}, {BASE_LON}")
print(requests.get(f"{BASE}/health", timeout=90).json())
send("1 normal report           ", make("1"))
send("2 same event_id (retry)   ", make("1"))
send("3 wrong device clock      ", make("3", recorded_at="1970-01-01T00:00:00Z"))
send("4 poor GPS (2 sats)       ", make("4", satellites=2, hdop=9.0))

batch = {"reports": [make("5"), make("6", latitude=999), make("7")]}
r = requests.post(f"{BASE}/reports/batch", json=batch, headers=HEADERS, timeout=90)
print("5 batch of 3               ->", r.status_code, r.json())