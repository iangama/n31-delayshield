import os, uuid, asyncio
from datetime import datetime, timezone, timedelta, date

import httpx
from celery import Celery
from sqlalchemy import create_engine, select, update, insert
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Text, Integer, DateTime, BigInteger, ForeignKey, Date
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.environ.get("DATABASE_URL")
OPENWEATHER_API_KEY_FILE = os.environ.get("OPENWEATHER_API_KEY_FILE", "/run/secrets/openweather_api_key")
OPENROUTESERVICE_API_KEY_FILE = os.environ.get("OPENROUTESERVICE_API_KEY_FILE", "/run/secrets/openrouteservice_api_key")
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")

OWM_DAILY_LIMIT = int(os.environ.get("OWM_DAILY_LIMIT", "800"))
ROUTE_DAILY_LIMIT = int(os.environ.get("ROUTE_DAILY_LIMIT", "400"))
OWM_PER_MIN_LIMIT = int(os.environ.get("OWM_PER_MIN_LIMIT", "30"))
ROUTE_PER_MIN_LIMIT = int(os.environ.get("ROUTE_PER_MIN_LIMIT", "20"))
SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", "60"))

if not DATABASE_URL:
  raise RuntimeError("DATABASE_URL required")

celery = Celery("worker", broker=REDIS_URL, backend=REDIS_URL)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase): pass

class Trip(Base):
  __tablename__ = "trips"
  id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
  deadline_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
  waypoints: Mapped[dict] = mapped_column(JSONB, nullable=False)

  eta_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=True)
  route_distance_m: Mapped[int] = mapped_column(Integer, nullable=True)
  route_duration_s: Mapped[int] = mapped_column(Integer, nullable=True)
  route_geojson: Mapped[dict] = mapped_column(JSONB, nullable=True)

  buffer_minutes: Mapped[int] = mapped_column(Integer, nullable=True)
  delay_risk_pct: Mapped[int] = mapped_column(Integer, nullable=True)
  status: Mapped[str] = mapped_column(Text, nullable=True)
  suggestion: Mapped[str] = mapped_column(Text, nullable=True)

  recommended_depart_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=True)
  why: Mapped[str] = mapped_column(Text, nullable=True)
  customer_message: Mapped[str] = mapped_column(Text, nullable=True)

  policy_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="balanced")
  trip_owm_daily_cap: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30")
  trip_route_daily_cap: Mapped[int] = mapped_column(Integer, nullable=False, server_default="15")
  next_calc_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=True)

  last_calc_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=True)
  calc_state: Mapped[str] = mapped_column(Text, nullable=False, server_default="idle")

class TripUpdate(Base):
  __tablename__ = "trip_updates"
  id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
  trip_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("trips.id", ondelete="CASCADE"), nullable=False)
  at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
  kind: Mapped[str] = mapped_column(Text, nullable=False)
  payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

class TripApiUsageDaily(Base):
  __tablename__ = "trip_api_usage_daily"
  trip_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("trips.id", ondelete="CASCADE"), primary_key=True)
  day: Mapped[date] = mapped_column(Date, primary_key=True)
  owm_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
  route_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

class ApiUsageDaily(Base):
  __tablename__ = "api_usage_daily"
  api_name: Mapped[str] = mapped_column(Text, primary_key=True)
  day: Mapped[date] = mapped_column(Date, primary_key=True)
  calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

class ApiUsageMinute(Base):
  __tablename__ = "api_usage_minute"
  api_name: Mapped[str] = mapped_column(Text, primary_key=True)
  minute_bucket: Mapped[object] = mapped_column(DateTime(timezone=True), primary_key=True)
  calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

def _read_secret(path: str) -> str | None:
  try:
    with open(path, "r", encoding="utf-8") as f:
      v = f.read().strip()
      return v or None
  except FileNotFoundError:
    return None

def _minute_bucket(dt: datetime) -> datetime:
  return dt.replace(second=0, microsecond=0)

def _ensure_usage_row_daily(db, api_name: str, d: date):
  row = db.scalar(select(ApiUsageDaily).where(ApiUsageDaily.api_name==api_name, ApiUsageDaily.day==d))
  if not row:
    db.execute(insert(ApiUsageDaily).values(api_name=api_name, day=d, calls=0))

def _ensure_usage_row_minute(db, api_name: str, mb: datetime):
  row = db.scalar(select(ApiUsageMinute).where(ApiUsageMinute.api_name==api_name, ApiUsageMinute.minute_bucket==mb))
  if not row:
    db.execute(insert(ApiUsageMinute).values(api_name=api_name, minute_bucket=mb, calls=0))

def _ensure_trip_usage_daily(db, trip_id: uuid.UUID, d: date):
  row = db.scalar(select(TripApiUsageDaily).where(TripApiUsageDaily.trip_id==trip_id, TripApiUsageDaily.day==d))
  if not row:
    db.execute(insert(TripApiUsageDaily).values(trip_id=trip_id, day=d, owm_calls=0, route_calls=0))

def _consume_budget(db, trip: Trip, api_name: str, kind: str, amount: int = 1) -> tuple[bool, str]:
  now = datetime.now(timezone.utc)
  d = date.today()
  mb = _minute_bucket(now)

  if api_name == "owm":
    daily_limit = OWM_DAILY_LIMIT
    per_min = OWM_PER_MIN_LIMIT
    trip_cap = int(trip.trip_owm_daily_cap)
  else:
    daily_limit = ROUTE_DAILY_LIMIT
    per_min = ROUTE_PER_MIN_LIMIT
    trip_cap = int(trip.trip_route_daily_cap)

  _ensure_usage_row_daily(db, api_name, d)
  _ensure_usage_row_minute(db, api_name, mb)
  _ensure_trip_usage_daily(db, trip.id, d)
  db.commit()

  gd = db.scalar(select(ApiUsageDaily).where(ApiUsageDaily.api_name==api_name, ApiUsageDaily.day==d).with_for_update())
  gm = db.scalar(select(ApiUsageMinute).where(ApiUsageMinute.api_name==api_name, ApiUsageMinute.minute_bucket==mb).with_for_update())
  tu = db.scalar(select(TripApiUsageDaily).where(TripApiUsageDaily.trip_id==trip.id, TripApiUsageDaily.day==d).with_for_update())

  if gd.calls + amount > daily_limit:
    return (False, f"global_daily_limit {api_name} {gd.calls}/{daily_limit}")
  if gm.calls + amount > per_min:
    return (False, f"per_min_limit {api_name} {gm.calls}/{per_min} bucket={mb.isoformat()}")
  if api_name == "owm" and (tu.owm_calls + amount > trip_cap):
    return (False, f"trip_daily_cap owm {tu.owm_calls}/{trip_cap}")
  if api_name == "route" and (tu.route_calls + amount > trip_cap):
    return (False, f"trip_daily_cap route {tu.route_calls}/{trip_cap}")

  gd.calls += amount
  gm.calls += amount
  if api_name == "owm": tu.owm_calls += amount
  else: tu.route_calls += amount
  db.commit()

  db.execute(insert(TripUpdate).values(trip_id=trip.id, kind="budget_consume", payload={"api": api_name, "kind": kind, "amount": amount}))
  db.commit()
  return (True, "ok")

async def _route(coords: list[list[float]]):
  ors = _read_secret(OPENROUTESERVICE_API_KEY_FILE)
  async with httpx.AsyncClient(timeout=25) as client:
    if ors:
      resp = await client.post(
        "https://api.openrouteservice.org/v2/directions/driving-car/geojson",
        headers={"Authorization": ors, "Content-Type":"application/json"},
        json={"coordinates": coords},
      )
      resp.raise_for_status()
      data = resp.json()
      feat = data["features"][0]
      s = feat["properties"]["summary"]
      return int(s["distance"]), int(s["duration"]), feat["geometry"], "ors"
    path = ";".join([f"{c[0]},{c[1]}" for c in coords])
    resp = await client.get(f"{OSRM_BASE_URL}/route/v1/driving/{path}", params={"overview":"full","geometries":"geojson"})
    resp.raise_for_status()
    data = resp.json()
    r = data["routes"][0]
    return int(r["distance"]), int(r["duration"]), r["geometry"], "osrm"

async def _forecast(lat: float, lon: float, eta_dt: datetime):
  key = _read_secret(OPENWEATHER_API_KEY_FILE)
  if not key:
    raise RuntimeError("OpenWeather key missing")
  async with httpx.AsyncClient(timeout=25) as client:
    resp = await client.get(
      "https://api.openweathermap.org/data/2.5/forecast",
      params={"lat": lat, "lon": lon, "appid": key, "units":"metric"},
    )
    resp.raise_for_status()
    data = resp.json()

  best = None; best_diff = None
  for item in data.get("list", []):
    dt = datetime.fromtimestamp(item["dt"], tz=timezone.utc)
    diff = abs((dt - eta_dt).total_seconds())
    if best is None or diff < best_diff:
      best, best_diff = item, diff
  if not best:
    return 0.0, {"summary":"no-forecast", "severity":0.0}

  wind = float(best.get("wind", {}).get("speed", 0.0))
  rain = float(best.get("rain", {}).get("3h", 0.0) or 0.0)
  snow = float(best.get("snow", {}).get("3h", 0.0) or 0.0)
  clouds = float(best.get("clouds", {}).get("all", 0.0))
  wx = (best.get("weather") or [{}])[0].get("main", "Unknown")

  sev = 0.0
  sev += min(1.0, rain/10.0) * 0.5
  sev += min(1.0, snow/5.0) * 0.6
  sev += min(1.0, wind/15.0) * 0.4
  sev += (clouds/100.0) * 0.1
  sev = max(0.0, min(1.0, sev))

  return sev, {
    "severity": sev,
    "wx": wx,
    "wind_mps": wind,
    "rain_3h_mm": rain,
    "snow_3h_mm": snow,
    "clouds_pct": clouds,
    "forecast_dt": datetime.fromtimestamp(best["dt"], tz=timezone.utc).isoformat()
  }

def _risk(deadline: datetime, eta: datetime, sev: float):
  slack_s = (deadline - eta).total_seconds()
  base = 0.10 if slack_s >= 4*3600 else (0.20 if slack_s >= 2*3600 else (0.40 if slack_s >= 0 else (0.70 if slack_s >= -2*3600 else 0.85)))
  risk = min(0.99, max(0.0, base + 0.25*sev))
  pct = int(round(risk*100))
  status = "🟢" if pct < 34 else ("🟡" if pct < 67 else "🔴")
  buffer_minutes = int(round(slack_s/60.0))
  why = f"buffer={buffer_minutes}min, weather_sev={sev:.2f}"
  if status == "🟢": sugg = "Manter rota. Recalcular mais perto do prazo."
  elif status == "🟡": sugg = "Considere antecipar saída e avisar cliente sobre possível variação."
  else: sugg = "ALTO risco: antecipar/alternar rota e ALERTAR cliente agora."
  return pct, status, buffer_minutes, why, sugg

def _recommend_depart(now: datetime, status: str, buffer_minutes: int):
  if status == "🟢": return now
  if status == "🟡": return now - timedelta(minutes=30 if buffer_minutes < 120 else 15)
  return now - timedelta(minutes=60 if buffer_minutes < 60 else 30)

def _customer_message(status: str, eta: datetime, deadline: datetime, why: str, suggestion: str):
  eta_s = eta.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
  dl_s = deadline.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
  return f"Atualização: status {status}. ETA {eta_s} (deadline {dl_s}). Motivo: {why}. Ação: {suggestion}"

def _next_interval_seconds(mode: str, status: str, budget_limited: bool):
  if budget_limited:
    return 45 * 60
  if mode == "conservative":
    return 60*60 if status=="🟢" else (25*60 if status=="🟡" else 8*60)
  if mode == "aggressive":
    return 20*60 if status=="🟢" else (8*60 if status=="🟡" else 2*60)
  return 40*60 if status=="🟢" else (15*60 if status=="🟡" else 5*60)

@celery.task(name="worker.tasks.scan_due_trips")
def scan_due_trips():
  now = datetime.now(timezone.utc)
  with Session() as db:
    rows = db.scalars(
      select(Trip).where(
        Trip.next_calc_at.isnot(None),
        Trip.next_calc_at <= now,
        Trip.calc_state.in_(["idle","done","budget_limited","error"])
      ).order_by(Trip.next_calc_at.asc()).limit(50)
    ).all()

    for t in rows:
      db.execute(update(Trip).where(Trip.id==t.id).values(calc_state="queued", next_calc_at=now + timedelta(seconds=SCAN_INTERVAL_SECONDS)))
      db.execute(insert(TripUpdate).values(trip_id=t.id, kind="recalc_queued", payload={"by":"scheduler"}))
      db.commit()
      celery.send_task("worker.tasks.recalc_trip", args=[str(t.id)])

  return {"ok": True, "queued": len(rows)}

@celery.task(name="worker.tasks.recalc_trip")
def recalc_trip(trip_id: str):
  tid = uuid.UUID(trip_id)
  now = datetime.now(timezone.utc)

  with Session() as db:
    t = db.scalar(select(Trip).where(Trip.id == tid))
    if not t: return {"ok": False, "error":"not-found"}

    db.execute(update(Trip).where(Trip.id==tid).values(calc_state="running"))
    db.execute(insert(TripUpdate).values(trip_id=tid, kind="recalc_running", payload={"at": now.isoformat()}))
    db.commit()

    points = t.waypoints.get("points") or []
    if len(points) < 2:
      db.execute(update(Trip).where(Trip.id==tid).values(calc_state="error"))
      db.execute(insert(TripUpdate).values(trip_id=tid, kind="recalc_error", payload={"stage":"validate","error":"need >=2 points"}))
      db.commit()
      return {"ok": False, "error":"bad-waypoints"}

    deadline = t.deadline_at
    need_route = True if (t.route_duration_s is None or t.route_geojson is None) else False

    coords = [[float(p["lon"]), float(p["lat"])] for p in points]
    dest_lat, dest_lon = float(points[-1]["lat"]), float(points[-1]["lon"])

    dist_m = t.route_distance_m
    dur_s = t.route_duration_s
    geom = t.route_geojson
    provider = "cached"

    if need_route:
      ok, reason = _consume_budget(db, t, "route", "route_calc", 1)
      if not ok:
        db.execute(update(Trip).where(Trip.id==tid).values(calc_state="budget_limited"))
        db.execute(insert(TripUpdate).values(trip_id=tid, kind="budget_denied", payload={"api":"route","reason":reason}))
        db.commit()
        next_s = _next_interval_seconds(t.policy_mode, t.status or "🟡", True)
        db.execute(update(Trip).where(Trip.id==tid).values(next_calc_at=now + timedelta(seconds=next_s), last_calc_at=now))
        db.commit()
        return {"ok": False, "error": "budget_denied_route", "reason": reason}

      try:
        dist_m, dur_s, geom, provider = asyncio.run(_route(coords))
      except Exception as e:
        db.execute(update(Trip).where(Trip.id==tid).values(calc_state="error"))
        db.execute(insert(TripUpdate).values(trip_id=tid, kind="recalc_error", payload={"stage":"route","error":str(e)}))
        db.commit()
        next_s = _next_interval_seconds(t.policy_mode, t.status or "🟡", False)
        db.execute(update(Trip).where(Trip.id==tid).values(next_calc_at=now + timedelta(seconds=next_s), last_calc_at=now))
        db.commit()
        return {"ok": False, "error": f"route: {str(e)}"}

    eta = now + timedelta(seconds=int(dur_s))

    ok, reason = _consume_budget(db, t, "owm", "weather_forecast", 1)
    if not ok:
      sev, wx = 0.0, {"severity":0.0, "budget_denied": True, "reason": reason}
      db.execute(insert(TripUpdate).values(trip_id=tid, kind="budget_denied", payload={"api":"owm","reason":reason}))
      db.commit()
      budget_limited = True
    else:
      try:
        sev, wx = asyncio.run(_forecast(dest_lat, dest_lon, eta))
        budget_limited = False
      except Exception as e:
        sev, wx = 0.0, {"severity":0.0, "error": str(e)}
        budget_limited = False

    risk_pct, status, buffer_minutes, why, suggestion = _risk(deadline, eta, sev)
    rec_depart = _recommend_depart(now, status, buffer_minutes)
    cust_msg = _customer_message(status, eta, deadline, why, suggestion)

    payload = {
      "route": {"distance_m": dist_m, "duration_s": dur_s, "geometry": geom, "provider": provider},
      "weather": wx,
      "buffer_minutes": buffer_minutes,
      "computed_at": now.isoformat(),
      "why": why
    }

    next_s = _next_interval_seconds(t.policy_mode, status, budget_limited)
    next_at = now + timedelta(seconds=next_s)

    with Session() as db2:
      db2.execute(update(Trip).where(Trip.id==tid).values(
        eta_at=eta,
        route_distance_m=dist_m,
        route_duration_s=dur_s,
        route_geojson=geom,
        buffer_minutes=buffer_minutes,
        delay_risk_pct=risk_pct,
        status=status,
        suggestion=suggestion,
        recommended_depart_at=rec_depart,
        why=why,
        customer_message=cust_msg,
        last_calc_at=now,
        next_calc_at=next_at,
        calc_state="budget_limited" if budget_limited else "done",
      ))
      db2.execute(insert(TripUpdate).values(trip_id=tid, kind="recalc_done", payload=payload))
      db2.commit()

  return {"ok": True, "risk_pct": risk_pct, "status": status, "budget_limited": budget_limited}
