"""
Enhanced Flask app that:
 - Uses OpenSky API with official client library
 - Polls for aircraft over Turkey with background thread
 - Checks recent flights for Israeli airports (requires auth)
 - Serves JSON at /api/turkey-israel-flights
 - Serves a Leaflet map at /

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
import logging

from flask import Flask, jsonify, render_template_string, request
from flask_cors import CORS
from shapely.geometry import Point, Polygon

# Use the official OpenSky Python client
from opensky_api import OpenSkyApi

# ===== CONFIG =====
OPENSKY_USERNAME = os.getenv("OPENSKY_USERNAME")
OPENSKY_PASSWORD = os.getenv("OPENSKY_PASSWORD")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "20"))
RECENT_WINDOW_HOURS = int(os.getenv("RECENT_WINDOW_HOURS", "6"))
MAX_AIRCRAFT_TO_QUERY = int(os.getenv("MAX_AIRCRAFT_TO_QUERY", "120"))

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

# Israeli airports for reference
ISRAELI_AIRPORTS = {
    'LLBG': 'Ben Gurion Airport',
    'LLIA': 'Ramon Airport', 
    'LLIB': 'Ovda Airport',
    'LLHB': 'Haifa Airport',
    'LLMZ': 'Tel Aviv (Sde Dov)',
    'LLES': 'Eilat Airport'
}

_api_lock = Lock()
_api = None  # lazy-init OpenSkyApi

_cache = {"ts": 0, "results": []}
_cache_lock = Lock()

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes


def get_api() -> OpenSkyApi:
    """Create a singleton OpenSkyApi client (thread-safe)."""
    global _api
    if _api is None:
        with _api_lock:
            if _api is None:
                if OPENSKY_USERNAME:
                    _api = OpenSkyApi(OPENSKY_USERNAME, OPENSKY_PASSWORD)
                    logger.info("Initialized OpenSky API with authentication")
                else:
                    _api = OpenSkyApi()
                    logger.warning("Using OpenSky API without authentication - limited functionality")
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
        hits.append({
            "icao24": s.icao24,
            "callsign": (s.callsign or "").strip(),
            "origin_country": s.origin_country,
            "lon": lon,
            "lat": lat,
            "altitude": s.geo_altitude or s.baro_altitude or 0,
            "velocity": s.velocity or 0,
            "heading": s.heading or 0,
        })
    return hits


def query_recent_flights(icao24: str, begin_ts: int, end_ts: int):
    """Use OpenSkyApi.get_flights_by_aircraft. Requires authenticated credentials for reliable results.
    Returns a list of dicts with minimal fields used by the frontend.
    """
    api = get_api()
    try:
        flights = api.get_flights_by_aircraft(icao24=icao24, begin=begin_ts, end=end_ts) or []
    except Exception as e:
        # Anonymous access is heavily rate-limited and flights endpoints may fail without auth
        logger.debug(f"Failed to get flights for {icao24}: {e}")
        return []

    out = []
    for f in flights:
        # The client returns objects with attributes like estDepartureAirport; keep this defensive
        dep = getattr(f, "estDepartureAirport", None)
        arr = getattr(f, "estArrivalAirport", None)
        first_seen = getattr(f, "firstSeen", None)
        last_seen = getattr(f, "lastSeen", None)
        out.append({
            "estDepartureAirport": dep,
            "estArrivalAirport": arr,
            "firstSeen": first_seen,
            "lastSeen": last_seen,
        })
    return out


def build_matching_list():
    """Build list of aircraft in Turkish airspace with Israeli connections."""
    try:
        state_vectors = fetch_states_over_turkey()
        logger.info(f"Fetched {len(state_vectors)} aircraft over Turkey")
    except Exception as e:
        logger.error("Fetch states error: %s", e)
        return []

    turkish_aircraft = aircraft_over_turkey(state_vectors)[:MAX_AIRCRAFT_TO_QUERY]
    logger.info(f"Found {len(turkish_aircraft)} aircraft in Turkish airspace")
    
    now = datetime.now(timezone.utc)
    end_ts = int(now.timestamp())
    begin_ts = int((now - timedelta(hours=RECENT_WINDOW_HOURS)).timestamp())

    matches: list[dict] = []
    
    for ac in turkish_aircraft:
        icao24 = ac["icao24"]
        callsign = ac["callsign"]
        
        try:
            flights = query_recent_flights(icao24, begin_ts, end_ts)
        except Exception as e:
            logger.debug(f"Error querying flights for {icao24}: {e}")
            flights = []
        
        matched_info = []
        for f in flights:
            dep = f.get("estDepartureAirport")
            arr = f.get("estArrivalAirport")
            if is_israel_airport(dep) or is_israel_airport(arr):
                matched_info.append({
                    "estDepartureAirport": dep,
                    "estArrivalAirport": arr,
                    "firstSeen": f.get("firstSeen"),
                    "lastSeen": f.get("lastSeen"),
                })
        
        if matched_info:
            matches.append({
                "icao24": icao24,
                "callsign": callsign,
                "lon": ac["lon"],
                "lat": ac["lat"],
                "altitude": int(ac["altitude"]),
                "speed": int(ac["velocity"]) if ac["velocity"] else 0,
                "heading": int(ac["heading"]) if ac["heading"] else 0,
                "origin_country": ac["origin_country"],
                "matched_flights": matched_info,
                "timestamp": time.time(),
                "last_seen": datetime.now(timezone.utc).isoformat()
            })
    
    logger.info(f"Found {len(matches)} Israeli-connected flights")
    return matches


def background_poller():
    """Background thread to continuously poll for flight data."""
    sleep_s = POLL_INTERVAL
    logger.info("Starting background poller")
    
    while True:
        try:
            new_results = build_matching_list()
            with _cache_lock:
                _cache["ts"] = time.time()
                _cache["results"] = new_results
            sleep_s = POLL_INTERVAL  # reset on success
            logger.info(f"Updated cache with {len(new_results)} flights")
        except Exception as e:
            logger.exception("Background poller error: %s", e)
            # back off a bit on errors
            sleep_s = min(max(int(sleep_s * 1.5), POLL_INTERVAL), 120)
        time.sleep(sleep_s)


@app.route("/api/turkey-israel-flights")
def api_flights():
    """API endpoint returning flights with Israeli connections in Turkish airspace."""
    if request.args.get("nocache") == "1":
        logger.info("Force refresh requested")
        results = build_matching_list()
        with _cache_lock:
            _cache["ts"] = time.time()
            _cache["results"] = results
    
    with _cache_lock:
        return jsonify({
            "fetched_at": int(_cache["ts"]),
            "count": len(_cache["results"]),
            "results": _cache["results"],
            "last_update": datetime.fromtimestamp(_cache["ts"], timezone.utc).isoformat() if _cache["ts"] else None
        })


@app.route("/api/flights")
def api_flights_simple():
    """Simple API endpoint compatible with original format."""
    if request.args.get("nocache") == "1":
        results = build_matching_list()
        with _cache_lock:
            _cache["ts"] = time.time()
            _cache["results"] = results
    
    with _cache_lock:
        # Convert to simple format
        flights = []
        for result in _cache["results"]:
            flights.append({
                "icao": result["icao24"],
                "callsign": result["callsign"],
                "lat": result["lat"],
                "lon": result["lon"],
                "altitude": result["altitude"],
                "speed": result["speed"],
                "heading": result["heading"],
                "timestamp": result["timestamp"],
                "last_seen": result["last_seen"]
            })
        
        return jsonify({
            "flights": flights,
            "count": len(flights),
            "last_update": datetime.fromtimestamp(_cache["ts"], timezone.utc).isoformat() if _cache["ts"] else None,
            "bounds": {
                "north": 42.5,
                "south": 35.0,
                "east": 45.5,
                "west": 25.0
            }
        })


@app.route('/health')
def health():
    """Health check endpoint"""
    with _cache_lock:
        cache_age = time.time() - _cache["ts"] if _cache["ts"] else float('inf')
    
    return jsonify({
        'status': 'healthy' if cache_age < 300 else 'stale',  # 5 minutes
        'cache_age_seconds': cache_age,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'cached_flights': len(_cache["results"]),
        'auth_configured': bool(OPENSKY_USERNAME)
    })


@app.route("/")
def index():
    """Map-based web interface with flight list similar to the original."""
    html_template = """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8"/>
      <title>Turkish airspace — aircraft from/to Israel</title>
      <meta name="viewport" content="width=device-width,initial-scale=1"/>
      <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
      <style>
        html,body{height:100%;margin:0;padding:0;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif}
        #container{display:flex;height:100%}
        #map{flex:3;position:relative}
        #list{flex:1;min-width:350px;overflow:auto;padding:0;background:#f8f9fa;border-left:2px solid #dee2e6;display:flex;flex-direction:column}
        
        #header{background:#1976d2;color:white;padding:1rem;text-align:center;box-shadow:0 2px 4px rgba(0,0,0,0.1)}
        #header h2{margin:0;font-size:1.3rem;font-weight:600}
        #header p{margin:0.5rem 0 0 0;font-size:0.9rem;opacity:0.9}
        
        #controls{padding:1rem;background:white;border-bottom:1px solid #dee2e6;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5rem}
        
        .refresh-btn{background:#28a745;color:white;border:none;padding:0.5rem 1rem;border-radius:4px;cursor:pointer;font-size:0.9rem;display:flex;align-items:center;gap:0.5rem;transition:background-color 0.2s}
        .refresh-btn:hover{background:#218838}
        .refresh-btn:disabled{background:#6c757d;cursor:not-allowed}
        
        .status{display:flex;gap:1rem;font-size:0.85rem;color:#6c757d}
        .stat{text-align:center}
        .stat-value{font-weight:bold;font-size:1.1rem;color:#1976d2}
        
        #flights-container{flex:1;padding:0;overflow-y:auto}
        
        .flight{margin:0;border-bottom:1px solid #dee2e6;padding:1rem;background:white;transition:background-color 0.2s;cursor:pointer}
        .flight:hover{background:#f8f9fa}
        
        .flight-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem}
        .flight-callsign{font-weight:bold;font-size:1.1rem;color:#1976d2}
        .flight-icao{font-family:monospace;background:#e9ecef;padding:0.2rem 0.5rem;border-radius:4px;font-size:0.8rem}
        
        .flight-info{font-size:0.9rem;color:#6c757d;margin-bottom:0.5rem}
        
        .routes{margin-top:0.5rem;padding-top:0.5rem;border-top:1px solid #eee}
        .route{display:flex;align-items:center;gap:0.5rem;margin:0.3rem 0;font-size:0.85rem}
        .airport{font-family:monospace;background:#f8f9fa;padding:0.2rem 0.4rem;border-radius:3px;font-weight:bold}
        .airport.israel{background:#e3f2fd;color:#1976d2}
        .route-arrow{color:#6c757d;font-weight:bold}
        
        .loading{text-align:center;padding:2rem;color:#6c757d}
        .spinner{display:inline-block;width:1rem;height:1rem;border:2px solid #f3f3f3;border-top:2px solid #1976d2;border-radius:50%;animation:spin 1s linear infinite}
        @keyframes spin{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}
        
        @media (max-width: 768px) {
            #container{flex-direction:column}
            #map{height:50%;flex:none}
            #list{flex:1;min-width:auto}
        }
      </style>
    </head>
    <body>
    <div id="container">
      <div id="map"></div>
      <div id="list">
        <div id="header">
            <h2>✈️ Turkish Airspace</h2>
            <p>Aircraft from/to Israel</p>
        </div>
        
        <div id="controls">
            <button class="refresh-btn" onclick="update()" id="refresh-btn">
                🔄 Refresh
            </button>
            <div class="status">
                <div class="stat">
                    <div class="stat-value" id="flight-count">-</div>
                    <div>Flights</div>
                </div>
                <div class="stat">
                    <div class="stat-value" id="last-update">-</div>
                    <div>Updated</div>
                </div>
            </div>
        </div>
        
        <div id="flights-container">
            <div class="loading">
                <div class="spinner"></div>
                <p>Loading flight data...</p>
            </div>
        </div>
      </div>
    </div>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
    const map=L.map('map').setView([39,34],6);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
        maxZoom:18, 
        attribution:'© OpenStreetMap contributors'
    }).addTo(map);
    
    // Add Turkey boundary outline
    L.polygon([
        [35.0, 25.0], [35.0, 45.5], [42.5, 45.5], [42.5, 25.0]
    ], {
        color: '#1976d2',
        weight: 2,
        opacity: 0.6,
        fillOpacity: 0.1
    }).addTo(map);
    
    let markers={};
    let isUpdating = false;
    
    // Custom airplane icon
    const airplaneIcon = L.divIcon({
        className: 'flight-marker',
        html: '✈️',
        iconSize: [24, 24],
        iconAnchor: [12, 12]
    });

    async function update(){
        if (isUpdating) return;
        
        isUpdating = true;
        const refreshBtn = document.getElementById('refresh-btn');
        refreshBtn.disabled = true;
        refreshBtn.innerHTML = '<span class="spinner"></span> Updating...';
        
        try{
            const res=await fetch('/api/turkey-israel-flights');
            const data=await res.json();
            
            // Update stats
            document.getElementById('flight-count').textContent = data.count;
            const lastUpdate = data.fetched_at ? new Date(data.fetched_at * 1000).toLocaleTimeString() : '-';
            document.getElementById('last-update').textContent = lastUpdate;
            
            const idsSeen=new Set();
            let listHtml='';
            
            for(const f of data.results){
                const id=f.icao24;
                idsSeen.add(id);
                
                const popup=`
                    <div style="font-family: sans-serif;">
                        <strong>${f.callsign||'(no callsign)'}</strong><br/>
                        <strong>ICAO24:</strong> ${f.icao24}<br/>
                        <strong>Alt:</strong> ${f.altitude.toLocaleString()} ft<br/>
                        <strong>Speed:</strong> ${f.speed} kts<br/>
                        <strong>Heading:</strong> ${f.heading}°<br/>
                        <strong>Matches:</strong> ${f.matched_flights.length}
                    </div>
                `;
                
                if(markers[id]){
                    markers[id].setLatLng([f.lat,f.lon]).getPopup().setContent(popup);
                } else {
                    markers[id]=L.marker([f.lat,f.lon], {icon: airplaneIcon})
                        .addTo(map).bindPopup(popup);
                }
                
                // Build routes info
                const routesInfo = f.matched_flights.map(m => {
                    const dep = m.estDepartureAirport || '?';
                    const arr = m.estArrivalAirport || '?';
                    const depClass = dep.startsWith('LL') ? ' israel' : '';
                    const arrClass = arr.startsWith('LL') ? ' israel' : '';
                    return `
                        <div class="route">
                            <span class="airport${depClass}">${dep}</span>
                            <span class="route-arrow">→</span>
                            <span class="airport${arrClass}">${arr}</span>
                        </div>
                    `;
                }).join('');
                
                listHtml+=`
                    <div class="flight" onclick="focusOnFlight('${id}')">
                        <div class="flight-header">
                            <div class="flight-callsign">${f.callsign||'(no callsign)'}</div>
                            <div class="flight-icao">${f.icao24}</div>
                        </div>
                        <div class="flight-info">
                            Alt: ${f.altitude.toLocaleString()}ft • Speed: ${f.speed}kts • ${f.origin_country}
                        </div>
                        <div class="routes">
                            ${routesInfo}
                        </div>
                    </div>
                `;
            }
            
            for(const id in markers){
                if(!idsSeen.has(id)){
                    map.removeLayer(markers[id]);
                    delete markers[id];
                }
            }
            
            document.getElementById('flights-container').innerHTML = 
                listHtml || '<div class="loading"><p>No matching flights right now.</p></div>';
                
        }catch(e){
            console.error("update failed",e);
            document.getElementById('flights-container').innerHTML = 
                '<div class="loading"><p style="color: red;">Error loading flight data. Please try again.</p></div>';
        }
        
        isUpdating = false;
        refreshBtn.disabled = false;
        refreshBtn.innerHTML = '🔄 Refresh';
    }
    
    function focusOnFlight(icao) {
        if (markers[icao]) {
            map.setView(markers[icao].getLatLng(), 10);
            markers[icao].openPopup();
        }
    }
    
    update();
    setInterval(update,30000);
    </script>
    </body>
    </html>
    """
    return render_template_string(html_template)


if __name__ == "__main__":
    # Start background polling thread
    t = Thread(target=background_poller, daemon=True)
    t.start()
    
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)