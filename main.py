import csv
import html
import io
import os
import secrets
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Query, Depends, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import create_engine, text, Column, Integer, Float, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

# ---------- Settings you can edit ----------
PROJECT_NAME = "TerraTrack"
PREPARED_BY = "Yantra Vidya"   # <- change this before submitting
IST = timezone(timedelta(hours=5, minutes=30))
METRIC_SRID = 32643   # UTM zone 43N: lets PostGIS measure distances in metres (suits Bengaluru/south India)

# ---------- Database (PostgreSQL + PostGIS) ----------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Set it to your Neon connection string.")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)  # Neon sleeps when idle
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class RoadEvent(Base):
    __tablename__ = "road_events"
    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String, index=True)
    latitude = Column(Float)
    longitude = Column(Float)
    vibration = Column(Float)
    event = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


# One-time setup that is safe to repeat on every start
with engine.begin() as conn:
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
Base.metadata.create_all(engine)
with engine.begin() as conn:
    # PostGIS point built automatically from latitude/longitude, so the two can never disagree
    conn.execute(text("""
        ALTER TABLE road_events
        ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)) STORED
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_road_events_geom ON road_events USING GIST (geom)"
    ))


# ---------- Validation ----------
class ReportIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=32)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    vibration: float = Field(ge=0)
    event: Literal["possible_pothole", "speed_breaker", "unknown"] = "possible_pothole"

    @model_validator(mode="after")
    def reject_no_gps_fix(self):
        if self.latitude == 0 and self.longitude == 0:
            raise ValueError("GPS has no valid fix (0, 0)")
        return self


# ---------- Helpers ----------
def severity_for(avg_vibration):
    if avg_vibration >= 3.5:
        return "High"
    if avg_vibration >= 2.5:
        return "Medium"
    return "Low"


def to_ist(dt):
    """Stored times are UTC; show them in Indian Standard Time."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p")


def maps_link(lat, lon):
    return f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}"


# PostGIS clusters the points: any chain of reports within :radius metres forms one group
CLUSTER_SQL = text("""
WITH pts AS (
    SELECT device_id, vibration, created_at, geom,
           ST_ClusterDBSCAN(ST_Transform(geom, :srid), :radius, 1) OVER () AS cid
    FROM road_events
    WHERE event = 'possible_pothole'
      AND (CAST(:cutoff AS timestamp) IS NULL OR created_at >= :cutoff)
)
SELECT ST_Y(ST_Centroid(ST_Collect(geom))) AS latitude,
       ST_X(ST_Centroid(ST_Collect(geom))) AS longitude,
       COUNT(*) AS report_count,
       COUNT(DISTINCT device_id) AS device_count,
       ROUND(AVG(vibration)::numeric, 2) AS avg_vibration,
       MAX(vibration) AS max_vibration,
       MIN(created_at) AS first_seen,
       MAX(created_at) AS last_seen
FROM pts
GROUP BY cid
""")


def run_clusters(radius_m=15, min_devices=2, days=None):
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    db = SessionLocal()
    try:
        rows = db.execute(
            CLUSTER_SQL, {"srid": METRIC_SRID, "radius": radius_m, "cutoff": cutoff}
        ).mappings().all()
    finally:
        db.close()

    result = []
    for r in rows:
        avg = float(r["avg_vibration"])
        result.append({
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "report_count": r["report_count"],
            "device_count": r["device_count"],
            "avg_vibration": avg,
            "max_vibration": r["max_vibration"],
            "severity": severity_for(avg),
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "status": "verified" if r["device_count"] >= min_devices else "unconfirmed",
        })
    return result


def analyse(days, radius_m, min_devices):
    """Collect everything the report needs."""
    db = SessionLocal()
    try:
        q = db.query(RoadEvent)
        if days:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
            q = q.filter(RoadEvent.created_at >= cutoff)
        rows = q.order_by(RoadEvent.id).all()
    finally:
        db.close()

    clusters = run_clusters(radius_m, min_devices, days)
    verified = sorted(
        [c for c in clusters if c["status"] == "verified"],
        key=lambda c: (c["avg_vibration"], c["device_count"]),
        reverse=True,
    )
    unconfirmed = sorted(
        [c for c in clusters if c["status"] == "unconfirmed"],
        key=lambda c: c["last_seen"],
        reverse=True,
    )
    return {
        "rows": rows,
        "event_counts": Counter(r.event for r in rows),
        "devices": sorted({r.device_id for r in rows}),
        "verified": verified,
        "unconfirmed": unconfirmed,
        "pothole_reports": sum(1 for r in rows if r.event == "possible_pothole"),
        "first": min((r.created_at for r in rows), default=None),
        "last": max((r.created_at for r in rows), default=None),
    }


# ---------- API ----------
app = FastAPI(title="TerraTrack API")

API_KEY = os.getenv("API_KEY")  # not set on your laptop, so no check locally


def check_api_key(x_api_key: Optional[str] = Header(None)):
    if API_KEY and not (x_api_key and secrets.compare_digest(x_api_key, API_KEY)):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse("dashboard.html")


@app.post("/report", status_code=201, dependencies=[Depends(check_api_key)])
def create_report(report: ReportIn):
    db = SessionLocal()
    try:
        row = RoadEvent(**report.model_dump())
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"status": "saved", "id": row.id}
    finally:
        db.close()


@app.get("/reports")
def list_reports(limit: int = 100):
    db = SessionLocal()
    try:
        rows = db.query(RoadEvent).order_by(RoadEvent.id.desc()).limit(limit).all()
        return [
            {
                "id": r.id,
                "device_id": r.device_id,
                "latitude": r.latitude,
                "longitude": r.longitude,
                "vibration": r.vibration,
                "event": r.event,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    finally:
        db.close()


@app.get("/potholes")
def potholes(radius_m: float = 15, min_devices: int = 2):
    out = []
    for c in run_clusters(radius_m, min_devices):
        c["first_seen"] = c["first_seen"].isoformat()
        c["last_seen"] = c["last_seen"].isoformat()
        out.append(c)
    return out


# ---------- Report ----------
REPORT_CSS = """
body{font-family:Arial,Helvetica,sans-serif;color:#111;max-width:980px;margin:24px auto;padding:0 16px}
h1{margin:0 0 4px;font-size:26px}
h2{margin-top:28px;border-bottom:2px solid #1f2937;padding-bottom:4px;font-size:18px}
.meta{color:#444;font-size:14px;line-height:1.6}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-top:12px}
.card{flex:1 1 140px;border:1px solid #ddd;border-radius:8px;padding:10px 14px;font-size:13px}
.card b{display:block;font-size:24px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
th,td{border:1px solid #ccc;padding:6px 8px;text-align:left}
th{background:#f3f4f6}
.sev{padding:2px 8px;border-radius:10px;color:#fff;font-size:12px}
.sev.high{background:#dc2626}.sev.medium{background:#d97706}.sev.low{background:#16a34a}
.actions{margin:16px 0}
.actions button,.actions a{display:inline-block;margin-right:8px;padding:8px 14px;border:0;border-radius:4px;
  background:#2563eb;color:#fff;text-decoration:none;font-size:14px;cursor:pointer}
.note{font-size:12.5px;color:#444;line-height:1.6}
@media print{.actions{display:none}body{margin:0}}
"""


def table_html(items):
    if not items:
        return "<p>None in this period.</p>"
    rows = []
    for i, c in enumerate(items, 1):
        rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{c['latitude']:.6f}, {c['longitude']:.6f}</td>"
            f"<td>{c['device_count']}</td>"
            f"<td>{c['report_count']}</td>"
            f"<td>{c['avg_vibration']}</td>"
            f"<td><span class='sev {c['severity'].lower()}'>{c['severity']}</span></td>"
            f"<td>{to_ist(c['last_seen'])}</td>"
            f"<td><a href='{maps_link(c['latitude'], c['longitude'])}' target='_blank'>Map</a></td>"
            "</tr>"
        )
    return (
        "<table><tr><th>#</th><th>Location (lat, lon)</th><th>Devices</th><th>Reports</th>"
        "<th>Avg vibration</th><th>Severity</th><th>Last reported (IST)</th><th>Link</th></tr>"
        + "".join(rows) + "</table>"
    )


@app.get("/pothole-report", response_class=HTMLResponse)
def pothole_report(
    days: Optional[int] = Query(None, ge=1, le=3650),
    radius_m: float = 15,
    min_devices: int = 2,
):
    d = analyse(days, radius_m, min_devices)
    verified, unconfirmed = d["verified"], d["unconfirmed"]

    params = {"radius_m": radius_m, "min_devices": min_devices}
    if days:
        params["days"] = days
    qs = html.escape(urlencode(params))

    if days:
        period = f"Last {days} day(s)"
    elif d["first"]:
        period = f"All recorded data ({to_ist(d['first'])} to {to_ist(d['last'])})"
    else:
        period = "No data recorded"

    sev = Counter(c["severity"] for c in verified)
    events = ", ".join(f"{html.escape(k)}: {v}" for k, v in d["event_counts"].items()) or "none"
    devices = ", ".join(html.escape(x) for x in d["devices"]) or "none"
    generated = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{PROJECT_NAME} Pothole Report</title>
<style>{REPORT_CSS}</style></head><body>
<div class="actions">
  <button onclick="window.print()">Print / Save as PDF</button>
  <a href="/pothole-report.csv?{qs}">Download CSV</a>
  <a href="/">Back to dashboard</a>
</div>
<h1>{PROJECT_NAME}: Road Anomaly (Pothole) Report</h1>
<div class="meta">
  Prepared by: {html.escape(PREPARED_BY)}<br>
  Generated: {generated}<br>
  Period: {period}<br>
  Verification rule: reports within {radius_m:g} m of one another are grouped (PostGIS spatial
  clustering); a location is <b>verified</b> when at least {min_devices} different devices reported it.
</div>

<h2>Summary</h2>
<div class="cards">
  <div class="card"><b>{len(d['rows'])}</b>Total sensor events</div>
  <div class="card"><b>{d['pothole_reports']}</b>Pothole reports</div>
  <div class="card"><b>{len(verified) + len(unconfirmed)}</b>Distinct locations</div>
  <div class="card"><b>{len(verified)}</b>Verified potholes</div>
  <div class="card"><b>{len(unconfirmed)}</b>Unconfirmed</div>
  <div class="card"><b>{len(d['devices'])}</b>Active devices</div>
</div>
<p class="meta">Verified by severity: High {sev.get('High', 0)}, Medium {sev.get('Medium', 0)},
Low {sev.get('Low', 0)}.<br>Event types: {events}.<br>Devices: {devices}.</p>

<h2>Verified potholes (highest severity first)</h2>
{table_html(verified)}

<h2>Unconfirmed locations (single device, need more reports or inspection)</h2>
{table_html(unconfirmed)}

<h2>Method and limitations</h2>
<p class="note">
Motorcycle-mounted MPU6050 sensors detect sudden vertical vibration and the ESP32 attaches the
latest GPS position. Severity is based on the average vibration measured at a location
(High &ge; 3.5, Medium &ge; 2.5, otherwise Low). These are potential road anomalies detected by
sensors, not confirmed damage: speed breakers, braking and GPS error can cause false reports.
Verified locations have repeated observations from different devices, and final confirmation
should be done by field inspection. Locations carry normal GPS error (a few metres).
</p>
</body></html>"""
    return HTMLResponse(page)


@app.get("/pothole-report.csv")
def pothole_report_csv(
    days: Optional[int] = Query(None, ge=1, le=3650),
    radius_m: float = 15,
    min_devices: int = 2,
):
    d = analyse(days, radius_m, min_devices)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["No", "Status", "Latitude", "Longitude", "Devices", "Reports",
                "Avg vibration", "Max vibration", "Severity",
                "First reported (IST)", "Last reported (IST)", "Google Maps link"])
    for i, c in enumerate(d["verified"] + d["unconfirmed"], 1):
        w.writerow([
            i, c["status"], f"{c['latitude']:.6f}", f"{c['longitude']:.6f}",
            c["device_count"], c["report_count"], c["avg_vibration"], c["max_vibration"],
            c["severity"], to_ist(c["first_seen"]), to_ist(c["last_seen"]),
            maps_link(c["latitude"], c["longitude"]),
        ])
    filename = f"terratrack_report_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )