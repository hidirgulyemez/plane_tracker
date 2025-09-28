"""
Flask app that:
 - polls OpenSky for aircraft over Turkey
 - checks recent flights for Israeli airports
 - serves JSON at /api/turkey-israel-flights
 - serves a Leaflet map at /

Deploy on Render/Heroku/Railway easily.
"""

import time
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string, request
from shapely.geometry import Point, Polygon
from threading import Lock, Thread
import os

# ===== CONFIG =====
OPENSKY_USERNAME = os.getenv("OPENSKY_USERNAME") 
OPENSKY_PASSWORD = os.getenv("OPENSKY_PASSWORD")  
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "20")) 
RECENT_WINDOW_HOURS = int(os.getenv("RECENT_WINDOW_HOURS", "6"))
MAX_AIRCRAFT_TO_QUERY = int(os.getenv("MAX_AIRCRAFT_TO_QUERY", "120"))

# Turkey bounding polygon (simplified)
TURKEY_POLY = Polygon([
    (25.0, 35.0),
    (45.5, 35.0),
    (45.5, 42.5),
    (25.0, 42.5),
])

def is_israel_airport(icao):
    return icao and icao.upper().startswith("LL")

STATES_URL = "https://opensky-network.org/api/states/all"
FLIGHTS_AIRCRAFT_URL = "https://opensky-network.org/api/flights/aircraft"

_cache = {"ts": 0, "results": []}
_cache_lock = Lock()

app = Flask(__name__)

def fetch_states():
    auth = (OPENSKY_USERNAME, OPENSKY_PASSWORD) if OPENSKY_USERNAME else None
    r = requests.get(STATES_URL, auth=auth, timeout=15)
    r.raise_for_status()
    return r.json()

def aircraft_over_turkey(states_json):
    hits = []
    states = states_json.get("states", []) or []
    for s in states:
        try:
            icao24 = s[0]
            callsign = (s[1] or "").strip()
            origin_country = s[2]
            lon = s[5]
            lat = s[6]
        except Exception:
            continue
        if lon is None or lat is None:
            continue
        if TURKEY_POLY.contains(Point(lon, lat)):
            hits.append({
                "icao24": icao24,
                "callsign": callsign,
                "origin_country": origin_country,
                "lon": lon,
                "lat": lat,
            })
    return hits

def query_recent_flights(icao24, begin_ts, end_ts):
    params = {"icao24": icao24, "begin": int(begin_ts), "end": int(end_ts)}
    auth = (OPENSKY_USERNAME, OPENSKY_PASSWORD) if OPENSKY_USERNAME else None
    r = requests.get(FLIGHTS_AIRCRAFT_URL, params=params, auth=auth, timeout=15)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json()

def build_matching_list():
    try:
        states_json = fetch_states()
    except Exception as e:
        app.logger.error("Fetch states error: %s", e)
        return []

    turkish_aircraft = aircraft_over_turkey(states_json)[:MAX_AIRCRAFT_TO_QUERY]
    now = datetime.now(timezone.utc)
    end_ts = int(now.timestamp())
    begin_ts = int((now - timedelta(hours=RECENT_WINDOW_HOURS)).timestamp())

    matches = []
    for ac in turkish_aircraft:
        icao24 = ac["icao24"]
        callsign = ac["callsign"]
        try:
            flights = query_recent_flights(icao24, begin_ts, end_ts)
        except Exception as e:
            flights = []
        matched_info = []
        for f in flights or []:
            dep = f.get("estDepartureAirport")
            arr = f.get("estArrivalAirport")
            if is_israel_airport(dep) or is_israel_airport(arr):
                matched_info.append({
                    "estDepartureAirport": dep,
                    "estArrivalAirport": arr,
                    "firstSeen": f.get("firstSeen"),
                    "lastSeen": f.get("lastSeen")
                })
        if matched_info:
            matches.append({
                "icao24": icao24,
                "callsign": callsign,
                "lon": ac["lon"],
                "lat": ac["lat"],
                "origin_country": ac["origin_country"],
                "matched_flights": matched_info
            })
    return matches

def background_poller():
    while True:
        try:
            new_results = build_matching_list()
            with _cache_lock:
                _cache["ts"] = time.time()
                _cache["results"] = new_results
        except Exception as e:
            app.logger.exception("Background poller error: %s", e)
        time.sleep(POLL_INTERVAL)

@app.route("/api/turkey-israel-flights")
def api_flights():
    if request.args.get("nocache") == "1":
        results = build_matching_list()
        with _cache_lock:
            _cache["ts"] = time.time()
            _cache["results"] = results
    with _cache_lock:
        return jsonify({
            "fetched_at": int(_cache["ts"]),
            "count": len(_cache["results"]),
            "results": _cache["results"]
        })

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
      // build list HTML
      const flightsInfo=f.matched_flights.map(m=>`${m.estDepartureAirport||'?' } → ${m.estArrivalAirport||'?'}`).join('<br/>');
      listHtml+=`<div class="flight"><strong>${f.callsign||'(no callsign)'}</strong><br/>
                 ICAO24: ${f.icao24}<br/>${flightsInfo}</div>`;
    }
    // remove stale markers
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

if __name__=="__main__":
    t=Thread(target=background_poller,daemon=True)
    t.start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=False)
