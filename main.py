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
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import create_engine, func, text, Column, Integer, Float, String, DateTime
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import declarative_base, sessionmaker

# ---------- Settings you can edit ----------
PROJECT_NAME = "TerraTrack"
PREPARED_BY = "Yantra Vidya"   # <- change this before submitting
IST = timezone(timedelta(hours=5, minutes=30))
METRIC_SRID = 32643          # UTM zone 43N: PostGIS measures distances in metres (suits south India)
MAX_BATCH = 50               # most events accepted in one /reports/batch call
EARLIEST_VALID_TIME = datetime(2024, 1, 1)   # device clocks before this are treated as wrong

# GPS-quality rules (override on Render with environment variables if you want to tune them)
MIN_SATELLITES = int(os.getenv("MIN_SATELLITES", "4"))
MAX_HDOP = float(os.getenv("MAX_HDOP", "5.0"))
MIN_SPEED_KMPH = float(os.getenv("MIN_SPEED_KMPH", "5"))


def utcnow():
    """Current UTC time without timezone info (how times are stored)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    event_id = Column(String)               # unique per device, used to ignore retries
    recorded_at = Column(DateTime)          # device (GPS) time, UTC
    latitude = Column(Float)
    longitude = Column(Float)
    vibration = Column(Float)
    speed_kmph = Column(Float)
    satellites = Column(Integer)
    hdop = Column(Float)
    event = Column(String)
    created_at = Column(DateTime, default=utcnow)   # when the server received it


# One-time setup that is safe to repeat on every start
with engine.begin() as conn:
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
Base.metadata.create_all(engine)
with engine.begin() as conn:
    # Upgrade tables created by earlier versions
    for ddl in (
        "ALTER TABLE road_events ADD COLUMN IF NOT EXISTS event_id VARCHAR",
        "ALTER TABLE road_events ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMP",
        "ALTER TABLE road_events ADD COLUMN IF NOT EXISTS speed_kmph DOUBLE PRECISION",
        "ALTER TABLE road_events ADD COLUMN IF NOT EXISTS satellites INTEGER",
        "ALTER TABLE road_events ADD COLUMN IF NOT EXISTS hdop DOUBLE PRECISION",
    ):
        conn.execute(text(ddl))
    # PostGIS point built automatically from latitude/longitude
    conn.execute(text("""
        ALTER TABLE road_events
        ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)) STORED
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_road_events_geom ON road_events USING GIST (geom)"
    ))
    # Same (device, event_id) can only be stored once; rows without event_id are unaffected
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_road_events_device_event "
        "ON road_events (device_id, event_id)"
    ))


# ---------- Validation ----------
class ReportIn(BaseModel):
    device_id: str = Field(min_length=1, max_length=32)
    event_id: Optional[str] = Field(None, min_length=1, max_length=64)
    recorded_at: Optional[datetime] = None      # ISO 8601 text (e.g. ...Z) or Unix seconds
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    vibration: float = Field(ge=0)
    speed_kmph: Optional[float] = Field(None, ge=0, le=300)
    satellites: Optional[int] = Field(None, ge=0, le=64)
    hdop: Optional[float] = Field(None, ge=0, le=100)
    event: Literal["possible_pothole", "speed_breaker", "unknown"] = "possible_pothole"

    @model_validator(mode="after")
    def reject_no_gps_fix(self):
        if self.latitude == 0 and self.longitude == 0:
            raise ValueError("GPS has no valid fix (0, 0)")
        return self


class BatchIn(BaseModel):
    reports: list[dict] = Field(min_length=1, max_length=MAX_BATCH)


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


def event_time(r):
    """When the pothole was hit: device time if it sent a sensible one, else arrival time."""
    return r.recorded_at or r.created_at


def passes_quality(r):
    return (
        (r.satellites is None or r.satellites >= MIN_SATELLITES)
        and (r.hdop is None or r.hdop <= MAX_HDOP)
        and (r.speed_kmph is None or r.speed_kmph >= MIN_SPEED_KMPH)
    )


def clean_recorded_at(dt):
    """Keep the device time only if it is plausible; a broken clock must not lose the report."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    if dt < EARLIEST_VALID_TIME or dt > utcnow() + timedelta(minutes=10):
        return None
    return dt


def store_report(db, rep):
    """Save one report. Returns ('saved', id) or ('duplicate', None)."""
    values = rep.model_dump(exclude={"recorded_at"})
    values["recorded_at"] = clean_recorded_at(rep.recorded_at)
    stmt = (
        pg_insert(RoadEvent)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["device_id", "event_id"])
        .returning(RoadEvent.id)
    )
    new_id = db.execute(stmt).scalar()
    db.commit()
    if new_id is None:
        return "duplicate", None
    return "saved", new_id


# PostGIS clusters the points: any chain of reports within :radius metres forms one group
CLUSTER_SQL = text("""
WITH pts AS (
    SELECT device_id, vibration, geom,
           COALESCE(recorded_at, created_at) AS event_time,
           ST_ClusterDBSCAN(ST_Transform(geom, :srid), :radius, 1) OVER () AS cid
    FROM road_events
    WHERE event = 'possible_pothole'
      AND (satellites IS NULL OR satellites >= :min_sat)
      AND (hdop IS NULL OR hdop <= :max_hdop)
      AND (speed_kmph IS NULL OR speed_kmph >= :min_speed)
      AND (CAST(:cutoff AS timestamp) IS NULL OR COALESCE(recorded_at, created_at) >= :cutoff)
)
SELECT ST_Y(ST_Centroid(ST_Collect(geom))) AS latitude,
       ST_X(ST_Centroid(ST_Collect(geom))) AS longitude,
       COUNT(*) AS report_count,
       COUNT(DISTINCT device_id) AS device_count,
       COUNT(DISTINCT CAST(event_time + INTERVAL '5 hours 30 minutes' AS date)) AS day_count,
       ROUND(AVG(vibration)::numeric, 2) AS avg_vibration,
       MAX(vibration) AS max_vibration,
       MIN(event_time) AS first_seen,
       MAX(event_time) AS last_seen
FROM pts
GROUP BY cid
""")


def run_clusters(radius_m=15, min_devices=2, min_days=2, days=None):
    cutoff = utcnow() - timedelta(days=days) if days else None
    db = SessionLocal()
    try:
        rows = db.execute(CLUSTER_SQL, {
            "srid": METRIC_SRID, "radius": radius_m, "cutoff": cutoff,
            "min_sat": MIN_SATELLITES, "max_hdop": MAX_HDOP, "min_speed": MIN_SPEED_KMPH,
        }).mappings().all()
    finally:
        db.close()

    result = []
    for r in rows:
        avg = float(r["avg_vibration"])
        by_devices = r["device_count"] >= min_devices
        by_repeat = min_days > 0 and r["day_count"] >= min_days
        if by_devices:
            basis = "multiple devices"
        elif by_repeat:
            basis = f"repeat sightings on {r['day_count']} different days"
        else:
            basis = ""
        result.append({
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "report_count": r["report_count"],
            "device_count": r["device_count"],
            "day_count": r["day_count"],
            "avg_vibration": avg,
            "max_vibration": r["max_vibration"],
            "severity": severity_for(avg),
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "status": "verified" if (by_devices or by_repeat) else "unconfirmed",
            "basis": basis,
        })
    return result


def analyse(days, radius_m, min_devices, min_days):
    """Collect everything the report needs."""
    db = SessionLocal()
    try:
        q = db.query(RoadEvent)
        if days:
            cutoff = utcnow() - timedelta(days=days)
            q = q.filter(func.coalesce(RoadEvent.recorded_at, RoadEvent.created_at) >= cutoff)
        rows = q.order_by(RoadEvent.id).all()
    finally:
        db.close()

    clusters = run_clusters(radius_m, min_devices, min_days, days)
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
        "excluded": sum(1 for r in rows if r.event == "possible_pothole" and not passes_quality(r)),
        "first": min((event_time(r) for r in rows), default=None),
        "last": max((event_time(r) for r in rows), default=None),
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


@app.get("/health")
def health():
    """Light check for devices and uptime pings; does not touch the database."""
    return {"status": "ok", "server_time_utc": utcnow().isoformat() + "Z"}


@app.post("/report", status_code=201, dependencies=[Depends(check_api_key)])
def create_report(report: ReportIn, response: Response):
    db = SessionLocal()
    try:
        outcome, new_id = store_report(db, report)
    finally:
        db.close()
    if outcome == "duplicate":
        response.status_code = 200      # already stored: the device can treat it as delivered
        return {"status": "duplicate"}
    return {"status": "saved", "id": new_id}


@app.post("/reports/batch", dependencies=[Depends(check_api_key)])
def create_batch(batch: BatchIn):
    """Send up to 50 queued events at once. Each is checked and answered separately."""
    counts = {"saved": 0, "duplicate": 0, "invalid": 0}
    results = []
    db = SessionLocal()
    try:
        for i, item in enumerate(batch.reports):
            try:
                rep = ReportIn.model_validate(item)
            except ValidationError as e:
                err = e.errors()[0]
                where = ".".join(str(x) for x in err["loc"]) or "report"
                counts["invalid"] += 1
                results.append({"index": i, "status": "invalid", "error": f"{where}: {err['msg']}"})
                continue
            outcome, new_id = store_report(db, rep)
            counts[outcome] += 1
            entry = {"index": i, "status": outcome}
            if new_id:
                entry["id"] = new_id
            results.append(entry)
    finally:
        db.close()
    return {**counts, "results": results}


@app.get("/reports")
def list_reports(limit: int = 100):
    db = SessionLocal()
    try:
        rows = db.query(RoadEvent).order_by(RoadEvent.id.desc()).limit(limit).all()
        return [
            {
                "id": r.id,
                "device_id": r.device_id,
                "event_id": r.event_id,
                "latitude": r.latitude,
                "longitude": r.longitude,
                "vibration": r.vibration,
                "speed_kmph": r.speed_kmph,
                "satellites": r.satellites,
                "hdop": r.hdop,
                "event": r.event,
                "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    finally:
        db.close()


@app.get("/potholes")
def potholes(radius_m: float = 15, min_devices: int = 2, min_days: int = 2):
    out = []
    for c in run_clusters(radius_m, min_devices, min_days):
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
            f"<td>{html.escape(c['basis']) or '-'}</td>"
            f"<td>{to_ist(c['last_seen'])}</td>"
            f"<td><a href='{maps_link(c['latitude'], c['longitude'])}' target='_blank'>Map</a></td>"
            "</tr>"
        )
    return (
        "<table><tr><th>#</th><th>Location (lat, lon)</th><th>Devices</th><th>Reports</th>"
        "<th>Avg vibration</th><th>Severity</th><th>Verified by</th>"
        "<th>Last reported (IST)</th><th>Link</th></tr>"
        + "".join(rows) + "</table>"
    )


@app.get("/pothole-report", response_class=HTMLResponse)
def pothole_report(
    days: Optional[int] = Query(None, ge=1, le=3650),
    radius_m: float = 15,
    min_devices: int = 2,
    min_days: int = 2,
):
    d = analyse(days, radius_m, min_devices, min_days)
    verified, unconfirmed = d["verified"], d["unconfirmed"]

    params = {"radius_m": radius_m, "min_devices": min_devices, "min_days": min_days}
    if days:
        params["days"] = days
    qs = html.escape(urlencode(params))

    if days:
        period = f"Last {days} day(s)"
    elif d["first"]:
        period = f"All recorded data ({to_ist(d['first'])} to {to_ist(d['last'])})"
    else:
        period = "No data recorded"

    repeat_rule = (
        f" or the same device reported it on at least {min_days} different days"
        if min_days > 0 else ""
    )
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
  clustering); a location is <b>verified</b> when at least {min_devices} different devices
  reported it{repeat_rule}.
</div>

<h2>Summary</h2>
<div class="cards">
  <div class="card"><b>{len(d['rows'])}</b>Total sensor events</div>
  <div class="card"><b>{d['pothole_reports']}</b>Pothole reports</div>
  <div class="card"><b>{d['excluded']}</b>Excluded (poor GPS / low speed)</div>
  <div class="card"><b>{len(verified) + len(unconfirmed)}</b>Distinct locations</div>
  <div class="card"><b>{len(verified)}</b>Verified potholes</div>
  <div class="card"><b>{len(unconfirmed)}</b>Unconfirmed</div>
  <div class="card"><b>{len(d['devices'])}</b>Active devices</div>
</div>
<p class="meta">Verified by severity: High {sev.get('High', 0)}, Medium {sev.get('Medium', 0)},
Low {sev.get('Low', 0)}.<br>Event types: {events}.<br>Devices: {devices}.</p>

<h2>Verified potholes (highest severity first)</h2>
{table_html(verified)}

<h2>Unconfirmed locations (need more reports or inspection)</h2>
{table_html(unconfirmed)}

<h2>Method and limitations</h2>
<p class="note">
Motorcycle-mounted MPU6050 sensors detect sudden vertical vibration and the ESP32 attaches the
latest GPS position, speed and GPS quality. Severity is based on the average vibration measured
at a location (High &ge; 3.5, Medium &ge; 2.5, otherwise Low). Reports with fewer than
{MIN_SATELLITES} satellites, HDOP above {MAX_HDOP:g}, or speed below {MIN_SPEED_KMPH:g} km/h
are kept in the database but excluded from the analysis. These are potential road anomalies
detected by sensors, not confirmed damage: speed breakers, braking and GPS error can cause
false reports, and a speed breaker that is crossed repeatedly can also look like a repeat
sighting. Final confirmation should be done by field inspection. Locations carry normal GPS
error (a few metres).
</p>
</body></html>"""
    return HTMLResponse(page)


@app.get("/pothole-report.csv")
def pothole_report_csv(
    days: Optional[int] = Query(None, ge=1, le=3650),
    radius_m: float = 15,
    min_devices: int = 2,
    min_days: int = 2,
):
    d = analyse(days, radius_m, min_devices, min_days)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["No", "Status", "Verified by", "Latitude", "Longitude", "Devices", "Days seen",
                "Reports", "Avg vibration", "Max vibration", "Severity",
                "First reported (IST)", "Last reported (IST)", "Google Maps link"])
    for i, c in enumerate(d["verified"] + d["unconfirmed"], 1):
        w.writerow([
            i, c["status"], c["basis"], f"{c['latitude']:.6f}", f"{c['longitude']:.6f}",
            c["device_count"], c["day_count"], c["report_count"], c["avg_vibration"],
            c["max_vibration"], c["severity"], to_ist(c["first_seen"]), to_ist(c["last_seen"]),
            maps_link(c["latitude"], c["longitude"]),
        ])
    filename = f"terratrack_report_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )