import random
import requests

for i in range(20):
    payload = {
        "device_id": f"TT00{random.randint(1, 3)}",
        "latitude": 12.9716 + random.uniform(-0.001, 0.001),
        "longitude": 77.5946 + random.uniform(-0.001, 0.001),
        "vibration": round(random.uniform(1.5, 4.0), 2),
        "event": "possible_pothole",
    }
    r = requests.post("http://127.0.0.1:8000/report", json=payload)
    print(r.status_code, r.json())