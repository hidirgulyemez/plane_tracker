"""
Flask app that:
 - polls OpenSky (via opensky_api lib) for aircraft over Turkey
 - checks recent flights for Israeli airports
 - serves JSON at /api/turkey-israel-flights
 - serves a Leaflet map at /

Ready for Render/Heroku/Railway. Requires Python 3.10+.

Env vars:
  OPENSKY_USERNAME, OPENSKY_PASSWORD   (recommended; needed for flights endpoints)
  POLL_INTERVAL                        (default 20s)
  RECENT_WINDOW_HOURS                  (default 6h)
  MAX_AIRCRAFT_TO_QUERY                (default 120)
  PORT                                 (Render assigns this)
"""

import time
from datetime import datetime, timezone, timedelta
from threading import Lock, Thread
import os

from flask import Flask, jsonify, render_template_string, request
from shapely.geometry import Point, Polygon

# Use the official OpenSky Python client
# pip install opensky-api
from opensky_api import OpenSkyApi

# ===== CONFIG =====
OPENSKY_USERNAME = os.getenv("OPENSKY_USERNAME")
OPENSKY_PASSWORD = os.getenv("OPENSKY_PASSWORD")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "20"))
RECENT_WINDOW_HOURS = int(os.getenv("RECENT_WINDOW_HOURS", "6"))
MAX_AIRCRAFT_TO_QUERY = int(os.getenv("MAX_AIRCRAFT_TO_QUERY", "120"))

# Turkey bounding polygon (rough bounding box, lon-lat order for shapely Point)
TURKEY_POLY = Polygon([
    (25.0, 35.0),
    (45.5, 35.0),
    (45.5, 42.5),
    (25.0, 42.5),
])

# Also define a bbox for server-side filtering in OpenSky API (lat_min, lat_max, lon_min, lon_max)
TURKEY_BBOX = (35.0, 42.5, 25.0, 45.5)

# ICAO prefix helper: Israeli airports start with "LL" (Turkey is "LT")
def is_israel_airport(icao: str | None) -> bool:
    return bool(icao) and icao.upper().startswith("LL")

_api_lock = Lock()
_api = None  # lazy-init OpenSkyApi

_cache = {"ts": 0, "results": []}
_cache_lock = Lock()

app = Flask(__name__)


def get_api() -> OpenSkyApi:
    """Create a singleton OpenSkyApi client (thread-safe)."""
    global _api
    if _api is None:
        with _api_lock:
            if _api is None:
                if OPENSKY_USERNAME:
                    _api = OpenSkyApi(OPENSKY_USERNAME, OPENSKY_PASSWORD)
                else:
                    _api = OpenSkyApi()
    return _api


def fetch_states_over_turkey():
    """Use OpenSkyApi.get_states with a Turkey bbox. Returns a list of StateVector objects."""
    api = get_api()
    # Using bbox reduces payload and rate-limit pressure
    states = api.get_states(bbox=TURKEY_BBOX)
    return states.states if states else []


def aircraft_over_turkey(state_vectors):
    """Project StateVector objects into a compact dict and keep those inside our polygon."""
    hits: list[dict] = []
    for s in state_vectors or []:
        lon = s.longitude
        lat = s.latitude
        if lon is None or lat is None:
            continue
        if not TURKEY_POLY.contains(Point(lon, lat)):
            continue
        hits.append(
            {
                "icao24": s.icao24,
                "callsign": (s.callsign or "").strip(),
                "origin_country": s.origin_country,
                "lon": lon,
                "lat": lat,
            }
        )
    return hits


def query_recent_flights(icao24: str, begin_ts: int, end_ts: int):
    """Use OpenSkyApi.get_flights_by_aircraft. Requires authenticated credentials for reliable results.
    Returns a list of dicts with minimal fields used by the frontend.
    """
    api = get_api()
    try:
        flights = api.get_flights_by_aircraft(icao24=icao24, begin=begin_ts, end=end_ts) or []
    except Exception:
        # Anonymous access is heavily rate-limited and flights endpoints may fail without auth
        return []

    out = []
    for f in flights:
        # The client returns objects with attributes like estDepartureAirport; keep this defensive
        dep = getattr(f, "estDepartureAirport", None)
        arr = getattr(f, "estArrivalAirport", None)
        first_seen = getattr(f, "firstSeen", None)
        last_seen = getattr(f, "lastSeen", None)
        out.append(
            {
                "estDepartureAirport": dep,
                "estArrivalAirport": arr,
                "firstSeen": first_seen,
                "lastSeen": last_seen,
            }
        )
    return out


def build_matching_list():
    try:
        state_vectors = fetch_states_over_turkey()
    except Exception as e:
        app.logger.error("Fetch states error: %s", e)
        return []

    turkish_aircraft = aircraft_over_turkey(state_vectors)[:MAX_AIRCRAFT_TO_QUERY]
    now = datetime.now(timezone.utc)
    end_ts = int(now.timestamp())
    begin_ts = int((now - timedelta(hours=RECENT_WINDOW_HOURS)).timestamp())

    matches: list[dict] = []
    for ac in turkish_aircraft:
        icao24 = ac["icao24"]
        callsign = ac["callsign"]
        try:
            flights = query_recent_flights(icao24, begin_ts, end_ts)
        except Exception:
            flights = []
        matched_info = []
        for f in flights:
            dep = f.get("estDepartureAirport")
            arr = f.get("estArrivalAirport")
            if is_israel_airport(dep) or is_israel_airport(arr):
                matched_info.append(
                    {
                        "estDepartureAirport": dep,
                        "estArrivalAirport": arr,
                        "firstSeen": f.get("firstSeen"),
                        "lastSeen": f.get("lastSeen"),
                    }
                )
        if matched_info:
            matches.append(
                {
                    "icao24": icao24,
                    "callsign": callsign,
                    "lon": ac["lon"],
                    "lat": ac["lat"],
                    "origin_country": ac["origin_country"],
                    "matched_flights": matched_info,
                }
            )
    return matches


def background_poller():
    # Simple backoff for rate limits
    sleep_s = POLL_INTERVAL
    while True:
        try:
            new_results = build_matching_list()
            with _cache_lock:
                _cache["ts"] = time.time()
                _cache["results"] = new_results
            sleep_s = POLL_INTERVAL  # reset on success
        except Exception as e:
            app.logger.exception("Background poller error: %s", e)
            # back off a bit on errors
            sleep_s = min(max(int(sleep_s * 1.5), POLL_INTERVAL), 120)
        time.sleep(sleep_s)


@app.route("/api/turkey-israel-flights")
def api_flights():
    if request.args.get("nocache") == "1":
        results = build_matching_list()
        with _cache_lock:
            _cache["ts"] = time.time()
            _cache["results"] = results
    with _cache_lock:
        return jsonify(
            {
                "fetched_at": int(_cache["ts"]),
                "count": len(_cache["results"]),
                "results": _cache["results"],
            }
        )


FRONTEND_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Turkish airspace — aircraft from/to Israel</title>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    html,body{height:100%;margin:0;padding:0}
    #container{display:flex;height:100%}
    #map{flex:3}
    #list{flex:1;overflow:auto;padding:0.5em;font-family:sans-serif;font-size:14px;background:#f9f9f9}
    #list h2{margin-top:0}
    .flight{margin-bottom:1em;border-bottom:1px solid #ccc;padding-bottom:0.5em}
  </style>
</head>
<body>
<div id="container">
  <div id="map"></div>
  <div id="list">
    <h2>Flights</h2>
    <div id="flights"></div>
  </div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const map=L.map('map').setView([39,34],5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:18, attribution:'© OpenStreetMap contributors'}).addTo(map);
let markers={};

async function update(){
  try{
    const res=await fetch('/api/turkey-israel-flights');
    const data=await res.json();
    const idsSeen=new Set();
    let listHtml='';
    for(const f of data.results){
      const id=f.icao24;
      idsSeen.add(id);
      const popup=`<b>${f.callsign||'(no callsign)'}</b><br/>icao24: ${f.icao24}<br/>matches: ${f.matched_flights.length}`;
      if(markers[id]){
        markers[id].setLatLng([f.lat,f.lon]).getPopup().setContent(popup);
      } else {
        markers[id]=L.marker([f.lat,f.lon]).addTo(map).bindPopup(popup);
      }
      const flightsInfo=f.matched_flights.map(m=>`${m.estDepartureAirport||'?' } → ${m.estArrivalAirport||'?'}`).join('<br/>');
      listHtml+=`<div class="flight"><strong>${f.callsign||'(no callsign)'}</strong><br/>ICAO24: ${f.icao24}<br/>${flightsInfo}</div>`;
    }
    for(const id in markers){
      if(!idsSeen.has(id)){
        map.removeLayer(markers[id]);
        delete markers[id];
      }
    }
    document.getElementById('flights').innerHTML=listHtml || '<p>No matching flights right now.</p>';
  }catch(e){console.error("update failed",e);}
}
update();
setInterval(update,15000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(FRONTEND_HTML)


if __name__ == "__main__":
    t = Thread(target=background_poller, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
