import os
import random
import sys

import requests

# Usage:  python fake_reports.py [LAT LON]
DEFAULT_LAT, DEFAULT_LON = 28.6139, 77.2090

if len(sys.argv) >= 3:
    BASE_LAT, BASE_LON = float(sys.argv[1]), float(sys.argv[2])
else:
    BASE_LAT, BASE_LON = DEFAULT_LAT, DEFAULT_LON

URL = os.getenv("TT_URL", "http://127.0.0.1:8000") + "/report"
HEADERS = {"X-API-Key": os.getenv("TT_API_KEY", "")}

print(f"Sending to {URL} around {BASE_LAT}, {BASE_LON}")

for i in range(20):
    payload = {
        "device_id": f"TT00{random.randint(1, 3)}",
        "latitude": BASE_LAT + random.uniform(-0.001, 0.001),
        "longitude": BASE_LON + random.uniform(-0.001, 0.001),
        "vibration": round(random.uniform(1.5, 4.0), 2),
        "event": "possible_pothole",
    }
    r = requests.post(URL, json=payload, headers=HEADERS, timeout=90)
    print(r.status_code, r.json())