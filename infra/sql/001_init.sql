CREATE TABLE IF NOT EXISTS trips (
  id UUID PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  deadline_at TIMESTAMPTZ NOT NULL,
  waypoints JSONB NOT NULL, -- { points: [{lat,lon},...] }

  eta_at TIMESTAMPTZ,
  route_distance_m INTEGER,
  route_duration_s INTEGER,
  route_geojson JSONB,

  buffer_minutes INTEGER,
  delay_risk_pct INTEGER,
  status TEXT,
  suggestion TEXT,

  recommended_depart_at TIMESTAMPTZ,
  why TEXT,
  customer_message TEXT,

  policy_mode TEXT NOT NULL DEFAULT 'balanced', -- conservative|balanced|aggressive
  trip_owm_daily_cap INTEGER NOT NULL DEFAULT 30,
  trip_route_daily_cap INTEGER NOT NULL DEFAULT 15,
  next_calc_at TIMESTAMPTZ,

  last_calc_at TIMESTAMPTZ,
  calc_state TEXT NOT NULL DEFAULT 'idle' -- idle|queued|running|done|error|budget_limited
);

CREATE TABLE IF NOT EXISTS trip_updates (
  id BIGSERIAL PRIMARY KEY,
  trip_id UUID NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  at TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS trip_api_usage_daily (
  trip_id UUID NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  day DATE NOT NULL,
  owm_calls INTEGER NOT NULL DEFAULT 0,
  route_calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (trip_id, day)
);

CREATE TABLE IF NOT EXISTS api_usage_daily (
  api_name TEXT NOT NULL,
  day DATE NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (api_name, day)
);

CREATE TABLE IF NOT EXISTS api_usage_minute (
  api_name TEXT NOT NULL,
  minute_bucket TIMESTAMPTZ NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (api_name, minute_bucket)
);

CREATE INDEX IF NOT EXISTS idx_trip_updates_trip_id_at ON trip_updates(trip_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_trips_next_calc_at ON trips(next_calc_at);

CREATE OR REPLACE FUNCTION touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_touch_trips ON trips;
CREATE TRIGGER trg_touch_trips
BEFORE UPDATE ON trips
FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
