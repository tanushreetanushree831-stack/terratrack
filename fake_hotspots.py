import os
import random
import sys

import requests

# Usage:  python fake_hotspots.py [LAT LON]      (latitude first, then longitude)
# With no numbers it uses central Delhi.
DEFAULT_LAT, DEFAULT_LON = 28.6139, 77.2090

if len(sys.argv) >= 3:
    BASE_LAT, BASE_LON = float(sys.argv[1]), float(sys.argv[2])
else:
    BASE_LAT, BASE_LON = DEFAULT_LAT, DEFAULT_LON

URL = os.getenv("TT_URL", "http://127.0.0.1:8000") + "/report"
HEADERS = {"X-API-Key": os.getenv("TT_API_KEY", "")}
J = 0.00004  # about 4-5 m of GPS jitter

print(f"Sending to {URL} around {BASE_LAT}, {BASE_LON}")


def send(device, lat, lon, vib):
    r = requests.post(URL, json={
        "device_id": device,
        "latitude": lat + random.uniform(-J, J),
        "longitude": lon + random.uniform(-J, J),
        "vibration": round(vib + random.uniform(-0.3, 0.3), 2),
        "event": "possible_pothole",
    }, headers=HEADERS, timeout=90)
    print(r.status_code, device)


# 3 real potholes: hit by several different bikes
hotspots = [(0.0010, 0.0005), (-0.0008, 0.0012), (0.0003, -0.0010)]
for dlat, dlon in hotspots:
    for device in ["TT001", "TT002", "TT003"][: random.randint(2, 3)]:
        send(device, BASE_LAT + dlat, BASE_LON + dlon, 3.5)

# 1 spot reported 5 times by the SAME bike -> should stay unconfirmed
for _ in range(5):
    send("TT001", BASE_LAT - 0.0015, BASE_LON - 0.0004, 3.0)

# 5 lone one-off reports from single devices
for _ in range(5):
    send(random.choice(["TT001", "TT002"]),
         BASE_LAT + random.uniform(-0.002, 0.002),
         BASE_LON + random.uniform(-0.002, 0.002),
         2.0)