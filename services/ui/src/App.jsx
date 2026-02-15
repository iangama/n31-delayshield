import React, { useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

const API = "/api";
const REFRESH_MS = 2500;

// OSM raster (sem key)
const OSM_RASTER_STYLE = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors"
    }
  },
  layers: [{ id: "osm", type: "raster", source: "osm" }]
};

function isoLocalFromISO(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

function statusClass(s) {
  if (s === "🟢") return "good";
  if (s === "🟡") return "warn";
  return "bad";
}

function clamp(n, a, b) { return Math.max(a, Math.min(b, n)); }
function round6(n) { return Math.round(n * 1e6) / 1e6; }

function normalizeWaypoints(raw) {
  // 1) força número
  const parsed = (raw || []).map(p => ({
    lat: Number(p.lat),
    lon: Number(p.lon)
  }));

  // 2) remove inválidos
  const valid = parsed.filter(p =>
    Number.isFinite(p.lat) &&
    Number.isFinite(p.lon) &&
    p.lat >= -90 && p.lat <= 90 &&
    p.lon >= -180 && p.lon <= 180
  );

  // 3) arredonda e remove duplicados consecutivos
  const uniq = [];
  for (const p of valid) {
    const q = { lat: round6(p.lat), lon: round6(p.lon) };
    const last = uniq[uniq.length - 1];
    if (!last || last.lat !== q.lat || last.lon !== q.lon) uniq.push(q);
  }

  return uniq;
}

async function readErr(res) {
  const text = await res.text().catch(() => "");
  // tenta extrair JSON do FastAPI
  try {
    const j = JSON.parse(text);
    return JSON.stringify(j);
  } catch {
    return text.slice(0, 240);
  }
}

export default function App() {
  const mapRef = useRef(null);
  const mapObjRef = useRef(null);

  const [trips, setTrips] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const activeTrip = useMemo(() => trips.find(t => t.id === activeId) || null, [trips, activeId]);

  // builder de waypoints (clique no mapa OU inputs numéricos)
  const [waypoints, setWaypoints] = useState([
    { lat: -19.9191, lon: -43.9386 },
    { lat: -23.5505, lon: -46.6333 }
  ]);

  const [deadlineLocal, setDeadlineLocal] = useState(() => {
    const d = new Date(Date.now() + 6 * 3600 * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  });

  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [lastPreviewPayload, setLastPreviewPayload] = useState(null);

  // --- API calls ---
  async function apiGet(path) {
    const r = await fetch(`${API}${path}`);
    if (!r.ok) throw new Error(`GET ${path} -> ${r.status} ${await readErr(r)}`);
    return await r.json();
  }
  async function apiPost(path, body) {
    const r = await fetch(`${API}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {})
    });
    if (!r.ok) throw new Error(`POST ${path} -> ${r.status} ${await readErr(r)}`);
    return await r.json();
  }

  async function loadTrips() {
    const data = await apiGet("/trips");
    const list = Array.isArray(data) ? data : (Array.isArray(data.items) ? data.items : []);
    setTrips(list);
    if (!activeId && list[0]?.id) setActiveId(list[0].id);
  }

  async function previewRoute() {
    setBusy(true); setMsg("");
    try {
      const clean = normalizeWaypoints(waypoints);

      if (clean.length < 2) {
        setMsg("Preview: precisa de pelo menos 2 waypoints válidos.");
        return;
      }

      // ORS costuma aceitar bem dezenas, mas para evitar erros:
      // Mantém início + fim + até 8 intermediários = máx 10
      const MAX_WP = 10;
      let clipped = clean;
      if (clean.length > MAX_WP) {
        clipped = [clean[0], ...clean.slice(1, MAX_WP - 1), clean[clean.length - 1]];
      }

      const payload = { waypoints: clipped };
      setLastPreviewPayload(payload);

      const data = await apiPost("/route/preview", payload);

      setPreview(data);
      drawRoute(data);
      setMsg(`Preview OK: ${Math.round(data.distance_m/1000)}km · ${Math.round(data.duration_s/60)}min (wps=${clipped.length})`);
    } catch (e) {
      setMsg(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function createTrip() {
    setBusy(true); setMsg("");
    try {
      const clean = normalizeWaypoints(waypoints);
      if (clean.length < 2) {
        setMsg("Criar viagem: precisa de pelo menos 2 waypoints válidos.");
        return;
      }

      const deadlineIso = new Date(deadlineLocal).toISOString();
      const created = await apiPost("/trips", { deadline_at: deadlineIso, waypoints: clean });

      setMsg(`Viagem criada: ${created.id} (recalc acionado)`);
      await loadTrips();
      setActiveId(created.id);

      await apiPost(`/trips/${created.id}/recalc`, {});
    } catch (e) {
      setMsg(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  async function recalcTrip(id) {
    if (!id) return;
    setBusy(true); setMsg("");
    try {
      await apiPost(`/trips/${id}/recalc`, {});
      setMsg("Recalculo solicitado. Aguarde o worker…");
    } catch (e) {
      setMsg(String(e.message || e));
    } finally {
      setBusy(false);
    }
  }

  // --- Map init ---
  useEffect(() => {
    if (!mapRef.current || mapObjRef.current) return;

    const m = new maplibregl.Map({
      container: mapRef.current,
      style: OSM_RASTER_STYLE,
      center: [-43.9386, -19.9191],
      zoom: 5
    });

    m.addControl(new maplibregl.NavigationControl(), "top-right");
    m.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }));

    m.on("load", () => {
      if (!m.getSource("wps")) {
        m.addSource("wps", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      }
      if (!m.getSource("route")) {
        m.addSource("route", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      }

      if (!m.getLayer("wps")) {
        m.addLayer({
          id: "wps",
          type: "circle",
          source: "wps",
          paint: {
            "circle-radius": 6,
            "circle-stroke-width": 2,
            "circle-stroke-color": "#0b1020",
            "circle-color": "#60a5fa"
          }
        });
      }
      if (!m.getLayer("route")) {
        m.addLayer({
          id: "route",
          type: "line",
          source: "route",
          paint: {
            "line-width": 4,
            "line-color": "#a78bfa"
          }
        });
      }

      renderWaypoints(m, normalizeWaypoints(waypoints));
    });

    m.on("click", (ev) => {
      const { lng, lat } = ev.lngLat;
      setWaypoints((prev) => {
        const next = [...prev];
        const p = { lat: round6(lat), lon: round6(lng) };
        if (ev.originalEvent.shiftKey && next.length >= 2) {
          next.splice(next.length - 1, 0, p);
        } else {
          next.push(p);
        }
        return next;
      });
    });

    mapObjRef.current = m;
    return () => m.remove();
  }, []);

  useEffect(() => {
    const m = mapObjRef.current;
    if (!m) return;
    renderWaypoints(m, normalizeWaypoints(waypoints));
  }, [waypoints]);

  // auto refresh trips
  useEffect(() => {
    let on = true;
    (async () => { try { await loadTrips(); } catch {} })();

    const t = setInterval(async () => {
      if (!on) return;
      try { await loadTrips(); } catch {}
    }, REFRESH_MS);

    return () => { on = false; clearInterval(t); };
  }, [activeId]);

  function renderWaypoints(m, wps) {
    const src = m.getSource("wps");
    if (!src) return;

    const features = (wps || []).map((p, idx) => ({
      type: "Feature",
      properties: { idx },
      geometry: { type: "Point", coordinates: [p.lon, p.lat] }
    }));
    src.setData({ type: "FeatureCollection", features });

    if (wps.length >= 2) {
      const lons = wps.map(p => p.lon);
      const lats = wps.map(p => p.lat);
      const minLon = Math.min(...lons), maxLon = Math.max(...lons);
      const minLat = Math.min(...lats), maxLat = Math.max(...lats);
      m.fitBounds([[minLon, minLat],[maxLon, maxLat]], { padding: 50, duration: 350 });
    }
  }

  function drawRoute(pre) {
    const m = mapObjRef.current;
    if (!m) return;
    const src = m.getSource("route");
    if (!src) return;

    const coords = pre?.geometry?.coordinates;
    if (!coords || !Array.isArray(coords) || coords.length < 2) return;

    src.setData({
      type: "FeatureCollection",
      features: [{
        type: "Feature",
        properties: {},
        geometry: { type: "LineString", coordinates: coords }
      }]
    });
  }

  function clearRoute() {
    const m = mapObjRef.current;
    if (m) {
      const src = m.getSource("route");
      if (src) src.setData({ type: "FeatureCollection", features: [] });
    }
    setPreview(null);
  }

  function setWP(idx, field, value) {
    setWaypoints(prev => {
      const next = [...prev];
      const p = { ...next[idx] };

      const num = Number(value);
      if (!Number.isFinite(num)) {
        p[field] = "";
      } else {
        if (field === "lat") p.lat = round6(clamp(num, -90, 90));
        if (field === "lon") p.lon = round6(clamp(num, -180, 180));
      }
      next[idx] = p;
      return next;
    });
  }

  function removeWP(idx) {
    setWaypoints(prev => {
      const next = prev.filter((_, i) => i !== idx);
      return next.length >= 2 ? next : prev;
    });
  }

  function resetTo2() {
    setWaypoints([
      { lat: -19.9191, lon: -43.9386 },
      { lat: -23.5505, lon: -46.6333 }
    ]);
    clearRoute();
  }

  const cleanCount = normalizeWaypoints(waypoints).length;

  return (
    <div className="container">
      <div className="card">
        <div className="h1">N31 DelayShield</div>
        <div className="sub">
          Clique no mapa para adicionar pontos. <span className="pill">Shift+Clique</span> insere parada antes do destino.
          Preview usa apenas waypoints válidos (lat [-90..90], lon [-180..180]).
        </div>

        <div className="hr" />

        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="pill">Waypoints: {waypoints.length} (válidos: {cleanCount})</div>
          <div className="row">
            <button className="btn ghost" onClick={resetTo2} disabled={busy}>Reset (BH→SP)</button>
            <button className="btn" onClick={previewRoute} disabled={busy || cleanCount < 2}>Preview rota</button>
          </div>
        </div>

        <div style={{ marginTop: 10, display:"flex", flexDirection:"column", gap:8 }}>
          {waypoints.map((p, idx) => (
            <div key={idx} className="row" style={{ alignItems:"flex-end" }}>
              <div style={{ flex:1 }}>
                <label>Lat #{idx+1}</label>
                <input
                  value={p.lat}
                  onChange={(e)=> setWP(idx,"lat", e.target.value)}
                  placeholder="-19.9191"
                />
              </div>
              <div style={{ flex:1 }}>
                <label>Lon #{idx+1}</label>
                <input
                  value={p.lon}
                  onChange={(e)=> setWP(idx,"lon", e.target.value)}
                  placeholder="-43.9386"
                />
              </div>
              <button className="btn ghost" onClick={()=>removeWP(idx)} disabled={busy || waypoints.length<=2}>
                Remover
              </button>
            </div>
          ))}
        </div>

        <div className="hr" />

        <label>Deadline (local)</label>
        <input value={deadlineLocal} onChange={(e)=>setDeadlineLocal(e.target.value)} type="datetime-local" />

        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn" onClick={createTrip} disabled={busy || cleanCount < 2}>Criar viagem</button>
          <button className="btn ghost" onClick={clearRoute} disabled={busy}>Limpar rota</button>
          <span className="pill">{busy ? "processando…" : "pronto"}</span>
        </div>

        {preview && (
          <div className="kpi">
            <div className="box">
              <div className="v">{Math.round(preview.distance_m/1000)} km</div>
              <div className="k">Distância</div>
            </div>
            <div className="box">
              <div className="v">{Math.round(preview.duration_s/60)} min</div>
              <div className="k">Duração (rota)</div>
            </div>
          </div>
        )}

        {msg && (
          <div style={{ marginTop: 10 }} className="help">
            <span className="code">{msg}</span>
          </div>
        )}

        {lastPreviewPayload && (
          <details style={{ marginTop: 10 }} className="help">
            <summary className="pill" style={{ cursor:"pointer" }}>debug: payload preview</summary>
            <pre className="code" style={{ whiteSpace:"pre-wrap", margin:0 }}>
{JSON.stringify(lastPreviewPayload, null, 2)}
            </pre>
          </details>
        )}

        <div className="hr" />

        <div className="h1" style={{ fontSize: 16, marginBottom: 6 }}>Viagens</div>

        <div className="list">
          {trips.length === 0 && <div className="small">Nenhuma viagem criada.</div>}

          {trips.map(t => (
            <div
              key={t.id}
              className={"item " + (t.id === activeId ? "active" : "")}
              onClick={()=>setActiveId(t.id)}
              role="button"
            >
              <div className="row" style={{ justifyContent:"space-between" }}>
                <div className="small">ID: <span className="code">{t.id}</span></div>
                <div className={"badge " + statusClass(t.status)}>{t.status || "—"}</div>
              </div>

              <div className="kpi">
                <div className="box">
                  <div className="v">{t.delay_risk_pct ?? "—"}%</div>
                  <div className="k">Risco de atraso</div>
                </div>
                <div className="box">
                  <div className="v">{t.buffer_minutes ?? "—"}</div>
                  <div className="k">Buffer (min)</div>
                </div>
                <div className="box">
                  <div className="v">{t.eta_at ? isoLocalFromISO(t.eta_at) : "—"}</div>
                  <div className="k">ETA atual</div>
                </div>
                <div className="box">
                  <div className="v">{t.deadline_at ? isoLocalFromISO(t.deadline_at) : "—"}</div>
                  <div className="k">Deadline</div>
                </div>
              </div>

              <div className="row" style={{ marginTop: 8, justifyContent:"space-between" }}>
                <div className="small">
                  Dist: {t.route_distance_m ? `${Math.round(t.route_distance_m/1000)}km` : "—"}
                  {" · "}
                  Dur: {t.route_duration_s ? `${Math.round(t.route_duration_s/60)}min` : "—"}
                </div>

                <button className="btn" onClick={(e)=>{ e.stopPropagation(); recalcTrip(t.id); }} disabled={busy}>
                  Recalcular agora
                </button>
              </div>

              {t.suggestion && (
                <div className="help" style={{ marginTop: 8 }}>
                  Sugestão: <b>{t.suggestion}</b>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className="mapwrap">
        <div className="map card" ref={mapRef} />
      </div>
    </div>
  );
}
