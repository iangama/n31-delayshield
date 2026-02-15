import os, uuid, asyncio
from datetime import datetime, timezone, date
from typing import Any, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select, insert, update
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Text, Integer, DateTime, BigInteger, ForeignKey, Date
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from celery import Celery
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
OPENROUTESERVICE_API_KEY_FILE = os.environ.get("OPENROUTESERVICE_API_KEY_FILE", "/run/secrets/openrouteservice_api_key")
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")

if not DATABASE_URL:
  raise RuntimeError("DATABASE_URL required")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
celery = Celery("api-enqueue", broker=REDIS_URL, backend=REDIS_URL)

REQS = Counter("api_requests_total", "API requests", ["path", "method", "code"])
LAT = Histogram("api_request_seconds", "API latency", ["path"])

def _read_secret(path: str) -> Optional[str]:
  try:
    with open(path, "r", encoding="utf-8") as f:
      v = f.read().strip()
      if not v:
        return None
      return v
  except FileNotFoundError:
    return None

class Base(DeclarativeBase): pass

class Trip(Base):
  __tablename__ = "trips"
  id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
  created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
  updated_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
  deadline_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=False)
  waypoints: Mapped[dict] = mapped_column(JSONB, nullable=False)

  eta_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
  route_distance_m: Mapped[int] = mapped_column(Integer, nullable=True)
  route_duration_s: Mapped[int] = mapped_column(Integer, nullable=True)
  route_geojson: Mapped[dict] = mapped_column(JSONB, nullable=True)

  buffer_minutes: Mapped[int] = mapped_column(Integer, nullable=True)
  delay_risk_pct: Mapped[int] = mapped_column(Integer, nullable=True)
  status: Mapped[str] = mapped_column(Text, nullable=True)
  suggestion: Mapped[str] = mapped_column(Text, nullable=True)

  recommended_depart_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
  why: Mapped[str] = mapped_column(Text, nullable=True)
  customer_message: Mapped[str] = mapped_column(Text, nullable=True)

  policy_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="balanced")
  trip_owm_daily_cap: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30")
  trip_route_daily_cap: Mapped[int] = mapped_column(Integer, nullable=False, server_default="15")
  next_calc_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)

  last_calc_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)
  calc_state: Mapped[str] = mapped_column(Text, nullable=False, server_default="idle")

class TripUpdate(Base):
  __tablename__ = "trip_updates"
  id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
  trip_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("trips.id", ondelete="CASCADE"), nullable=False)
  at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
  kind: Mapped[str] = mapped_column(Text, nullable=False)
  payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

class TripApiUsageDaily(Base):
  __tablename__ = "trip_api_usage_daily"
  trip_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("trips.id", ondelete="CASCADE"), primary_key=True)
  day: Mapped[date] = mapped_column(Date, primary_key=True)
  owm_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
  route_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

class Waypoint(BaseModel):
  lat: float
  lon: float

class RoutePreviewIn(BaseModel):
  waypoints: List[Waypoint] = Field(min_length=2)

class TripCreateIn(BaseModel):
  deadline_at: datetime
  waypoints: List[Waypoint] = Field(min_length=2)
  policy_mode: str = "balanced"
  trip_owm_daily_cap: int = 30
  trip_route_daily_cap: int = 15

class TripPolicyPatch(BaseModel):
  policy_mode: Optional[str] = None
  trip_owm_daily_cap: Optional[int] = None
  trip_route_daily_cap: Optional[int] = None

class TripOut(BaseModel):
  id: uuid.UUID
  created_at: datetime
  updated_at: datetime
  deadline_at: datetime
  waypoints: List[Waypoint]

  eta_at: Optional[datetime] = None
  route_distance_m: Optional[int] = None
  route_duration_s: Optional[int] = None

  buffer_minutes: Optional[int] = None
  delay_risk_pct: Optional[int] = None
  status: Optional[str] = None
  suggestion: Optional[str] = None

  recommended_depart_at: Optional[datetime] = None
  why: Optional[str] = None
  customer_message: Optional[str] = None

  policy_mode: str
  trip_owm_daily_cap: int
  trip_route_daily_cap: int
  next_calc_at: Optional[datetime] = None

  last_calc_at: Optional[datetime] = None
  calc_state: str

class TripWithHistory(BaseModel):
  trip: TripOut
  updates: list[dict]
  usage_today: dict

app = FastAPI(title="DelayShield API", version="4.0.0")

@app.middleware("http")
async def metrics_mw(request, call_next):
  path = request.url.path
  method = request.method
  with LAT.labels(path=path).time():
    resp = await call_next(request)
  REQS.labels(path=path, method=method, code=str(resp.status_code)).inc()
  return resp

@app.get("/metrics")
def metrics():
  return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health():
  return {"ok": True}

async def _route_from_waypoints(waypoints: list[Waypoint]) -> dict:
  coords = [[w.lon, w.lat] for w in waypoints]
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
      return {"distance_m": int(s["distance"]), "duration_s": int(s["duration"]), "geometry": feat["geometry"], "provider":"ors"}
    else:
      path = ";".join([f"{w.lon},{w.lat}" for w in waypoints])
      resp = await client.get(f"{OSRM_BASE_URL}/route/v1/driving/{path}", params={"overview":"full","geometries":"geojson"})
      resp.raise_for_status()
      data = resp.json()
      r = data["routes"][0]
      return {"distance_m": int(r["distance"]), "duration_s": int(r["duration"]), "geometry": r["geometry"], "provider":"osrm"}

def _push_update(db, trip_id: uuid.UUID, kind: str, payload: dict):
  db.execute(insert(TripUpdate).values(trip_id=trip_id, kind=kind, payload=payload))

def _to_out(t: Trip) -> TripOut:
  wps = [Waypoint(**p) for p in (t.waypoints.get("points") or [])]
  return TripOut(
    id=t.id, created_at=t.created_at, updated_at=t.updated_at,
    deadline_at=t.deadline_at, waypoints=wps,
    eta_at=t.eta_at, route_distance_m=t.route_distance_m, route_duration_s=t.route_duration_s,
    buffer_minutes=t.buffer_minutes, delay_risk_pct=t.delay_risk_pct, status=t.status, suggestion=t.suggestion,
    recommended_depart_at=t.recommended_depart_at, why=t.why, customer_message=t.customer_message,
    policy_mode=t.policy_mode, trip_owm_daily_cap=t.trip_owm_daily_cap, trip_route_daily_cap=t.trip_route_daily_cap,
    next_calc_at=t.next_calc_at, last_calc_at=t.last_calc_at, calc_state=t.calc_state
  )

def _validate_mode(m: str):
  if m not in ("conservative","balanced","aggressive"):
    raise HTTPException(status_code=400, detail="policy_mode must be conservative|balanced|aggressive")

@app.post("/api/route/preview")
def route_preview(body: RoutePreviewIn):
  try:
    out = asyncio.run(_route_from_waypoints(body.waypoints))
    return out
  except Exception as e:
    raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/trips", response_model=TripOut)
def create_trip(body: TripCreateIn):
  _validate_mode(body.policy_mode)
  tid = uuid.uuid4()
  wp_payload = {"points": [w.model_dump() for w in body.waypoints]}
  now = datetime.now(timezone.utc)

  with Session() as db:
    db.execute(insert(Trip).values(
      id=tid,
      deadline_at=body.deadline_at,
      waypoints=wp_payload,
      policy_mode=body.policy_mode,
      trip_owm_daily_cap=int(body.trip_owm_daily_cap),
      trip_route_daily_cap=int(body.trip_route_daily_cap),
      next_calc_at=now,
      calc_state="queued",
    ))
    _push_update(db, tid, "created", {"deadline_at": body.deadline_at.isoformat(), "waypoints_n": len(body.waypoints), "policy_mode": body.policy_mode})
    _push_update(db, tid, "recalc_queued", {"by":"create"})
    db.commit()

  celery.send_task("worker.tasks.recalc_trip", args=[str(tid)])

  with Session() as db:
    t = db.scalar(select(Trip).where(Trip.id == tid))
    return _to_out(t)

@app.get("/api/trips", response_model=list[TripOut])
def list_trips():
  with Session() as db:
    rows = db.scalars(select(Trip).order_by(Trip.created_at.desc())).all()
    return [_to_out(t) for t in rows]

@app.get("/api/trips/{trip_id}", response_model=TripWithHistory)
def get_trip(trip_id: uuid.UUID):
  today = date.today()
  with Session() as db:
    t = db.scalar(select(Trip).where(Trip.id == trip_id))
    if not t: raise HTTPException(status_code=404, detail="not found")
    ups = db.execute(select(TripUpdate).where(TripUpdate.trip_id==trip_id).order_by(TripUpdate.at.desc()).limit(60)).scalars().all()
    u = db.scalar(select(TripApiUsageDaily).where(TripApiUsageDaily.trip_id==trip_id, TripApiUsageDaily.day==today))
    usage = {"owm_calls": (u.owm_calls if u else 0), "route_calls": (u.route_calls if u else 0),
             "owm_cap": t.trip_owm_daily_cap, "route_cap": t.trip_route_daily_cap}
    return {"trip": _to_out(t), "updates": [{"id":u2.id,"at":u2.at,"kind":u2.kind,"payload":u2.payload or {}} for u2 in ups], "usage_today": usage}

@app.post("/api/trips/{trip_id}/recalc", response_model=TripOut)
def recalc_now(trip_id: uuid.UUID):
  with Session() as db:
    t = db.scalar(select(Trip).where(Trip.id==trip_id))
    if not t: raise HTTPException(status_code=404, detail="not found")
    db.execute(update(Trip).where(Trip.id==trip_id).values(calc_state="queued", next_calc_at=datetime.now(timezone.utc)))
    _push_update(db, trip_id, "recalc_queued", {"by":"user"})
    db.commit()

  celery.send_task("worker.tasks.recalc_trip", args=[str(trip_id)])

  with Session() as db:
    t2 = db.scalar(select(Trip).where(Trip.id==trip_id))
    return _to_out(t2)

@app.patch("/api/trips/{trip_id}/policy", response_model=TripOut)
def patch_policy(trip_id: uuid.UUID, body: TripPolicyPatch):
  with Session() as db:
    t = db.scalar(select(Trip).where(Trip.id==trip_id))
    if not t: raise HTTPException(status_code=404, detail="not found")
    vals = {}
    if body.policy_mode is not None:
      _validate_mode(body.policy_mode)
      vals["policy_mode"] = body.policy_mode
    if body.trip_owm_daily_cap is not None:
      vals["trip_owm_daily_cap"] = int(body.trip_owm_daily_cap)
    if body.trip_route_daily_cap is not None:
      vals["trip_route_daily_cap"] = int(body.trip_route_daily_cap)
    if not vals:
      return _to_out(t)
    db.execute(update(Trip).where(Trip.id==trip_id).values(**vals))
    _push_update(db, trip_id, "policy_updated", vals)
    db.commit()
  with Session() as db:
    t2 = db.scalar(select(Trip).where(Trip.id==trip_id))
    return _to_out(t2)
