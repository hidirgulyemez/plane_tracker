# Israel-Turkey Flight Tracker

Real-time tracking of Israeli flights in Turkish airspace using OpenSky Network data.

![Flight Tracker Screenshot](https://via.placeholder.com/800x400/1976d2/ffffff?text=Flight+Tracker+Interface)

## Features

- 🗺️ **Interactive Map**: Leaflet-based map with aircraft markers
- ✈️ **Real-time Data**: OpenSky Network API integration with background polling
- 🇮🇱 **Israeli Flight Detection**: Identifies flights with Israeli airport connections
- 📊 **Flight Details**: Shows callsign, altitude, speed, heading, and route information
- 🔄 **Auto-refresh**: Updates every 30 seconds automatically
- 📱 **Responsive Design**: Works on desktop and mobile devices

## Quick Start

### Local Development

1. **Clone and Setup**
   ```bash
   git clone <your-repo-url>
   cd israel-turkey-flight-tracker
   pip install -r requirements.txt
   ```

2. **Set Environment Variables** (Optional but recommended)
   ```bash
   export OPENSKY_USERNAME="your-username"
   export OPENSKY_PASSWORD="your-password"
   ```

3. **Run the Application**
   ```bash
   python app.py
   ```

4. **Access the Interface**
   - Web Interface: http://localhost:5000
   - API Endpoint: http://localhost:5000/api/turkey-israel-flights

### Deploy on Render

1. **Push to GitHub**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin <your-github-repo-url>
   git push -u origin main
   ```

2. **Deploy on Render**
   - Go to [Render Dashboard](https://dashboard.render.com/)
   - Click "New +" → "Web Service"
   - Connect your GitHub repository
   - Configure:
     - **Name**: `israel-turkey-flight-tracker`
     - **Environment**: Python 3
     - **Build Command**: `pip install -r requirements.txt`
     - **Start Command**: `gunicorn app:app`

3. **Set Environment Variables** (Optional)
   - `OPENSKY_USERNAME`: Your OpenSky Network username
   - `OPENSKY_PASSWORD`: Your OpenSky Network password
   - `POLL_INTERVAL`: Polling frequency in seconds (default: 20)
   - `RECENT_WINDOW_HOURS`: Flight history window (default: 6)

## API Endpoints

### GET /api/turkey-israel-flights
Returns detailed flight data with route information.

**Response:**
```json
{
  "fetched_at": 1696003200,
  "count": 2,
  "results": [
    {
      "icao24": "738c12",
      "callsign": "ELY123",
      "lat": 40.1234,
      "lon": 32.5678,
      "altitude": 35000,
      "speed": 450,
      "heading": 290,
      "origin_country": "Israel",
      "matched_flights": [
        {
          "estDepartureAirport": "LLBG",
          "estArrivalAirport": "LTFM",
          "firstSeen": 1695999600,
          "lastSeen": 1696003200
        }
      ]
    }
  ]
}
```

### GET /api/flights
Simple format compatible with basic applications.

### GET /health
Health check endpoint for monitoring.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENSKY_USERNAME` | - | OpenSky Network username (optional) |
| `OPENSKY_PASSWORD` | - | OpenSky Network password (optional) |
| `POLL_INTERVAL` | 20 | Background polling frequency (seconds) |
| `RECENT_WINDOW_HOURS` | 6 | Flight history lookup window (hours) |
| `MAX_AIRCRAFT_TO_QUERY` | 120 | Maximum aircraft to check per update |
| `PORT` | 5000 | Server port (automatically set by Render) |

### OpenSky Network Authentication

While the application works without authentication, providing OpenSky credentials enables:
- ✅ Full flight history access
- ✅ Better rate limits
- ✅ More reliable route detection
- ✅ Access to departure/arrival airport data

Without authentication:
- ⚠️ Limited flight history access
- ⚠️ Higher rate limits
- ⚠️ Reduced route detection accuracy

### Get OpenSky Credentials

1. Visit [OpenSky Network](https://opensky-network.org/)
2. Create a free account
3. Use your username/password as environment variables

## Technical Details

### Architecture

- **Backend**: Flask with background polling threads
- **Frontend**: Vanilla JavaScript with Leaflet maps
- **Data Source**: OpenSky Network REST API
- **Geospatial**: Shapely for Turkish airspace boundary detection
- **Deployment**: WSGI-compatible (Render, Heroku, Railway)

### Flight Detection Logic

1. **Fetch Aircraft**: Query OpenSky for aircraft in Turkish airspace bounding box
2. **Filter Geographically**: Use Shapely polygon to precisely filter Turkish airspace
3. **Check Flight History**: Query recent flights (6 hours) for each aircraft
4. **Match Israeli Connections**: Identify flights with ICAO codes starting with "LL"
5. **Cache Results**: Store in memory with background updates every 20 seconds

### Turkish Airspace Boundaries

The application uses approximate boundaries:
- **North**: 42.5°
- **South**: 35.0°
- **East**: 45.5°
- **West**: 25.0°

### Israeli Airports (ICAO: LL*)

- **LLBG**: Ben Gurion Airport (Tel Aviv)
- **LLIA**: Ramon Airport
- **LLIB**: Ovda Airport
- **LLHB**: Haifa Airport
- **LLMZ**: Tel Aviv (Sde Dov)
- **LLES**: Eilat Airport

## Troubleshooting

### Common Issues

1. **No Flights Detected**
   ```
   Solution: Check if there are actually Israeli flights in Turkish airspace
   - Try manual refresh
   - Check OpenSky service status
   - Verify API credentials
   ```

2. **API Rate Limiting**
   ```
   Solution: The app includes automatic backoff
   - Uses authenticated requests when possible
   - Caches results to reduce API calls
   - Implements exponential backoff on errors
   ```

3. **Deployment Issues**
   ```
   Solution: Check logs and configuration
   - Verify all files are committed to Git
   - Check environment variables in Render
   - Review build and runtime logs
   ```

### Monitoring

- **Health Endpoint**: `/health` shows system status
- **Cache Status**: Displays data freshness and authentication status
- **Logs**: Application logs show API calls and errors

## Legal and Ethical Considerations

### ✅ Permitted Uses
- Educational and research purposes
- Public flight tracking (ADS-B data is publicly broadcast)
- Non-commercial personal use
- Open source development

### 📋 Data Sources
- Uses only publicly available ADS-B transponder data
- No classified or restricted flight information
- Complies with OpenSky Network terms of service

### 🔒 Privacy and Security
- No personal data collection
- Aircraft positions are already public via ADS-B
- Respects API rate limits and terms of service

## Contributing

1. **Fork the Repository**
2. **Create Feature Branch**: `git checkout -b feature/amazing-feature`
3. **Commit Changes**: `git commit -m 'Add amazing feature'`
4. **Push to Branch**: `git push origin feature/amazing-feature`
5. **Open Pull Request**

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

- **Issues**: Report bugs via GitHub Issues
- **Documentation**: Check the README and code comments
- **OpenSky API**: [OpenSky Network Documentation](https://opensky-network.org/apidoc/)
- **Render Deployment**: [Render Documentation](https://render.com/docs)

## Changelog

### v1.0.0
- Initial release with OpenSky integration
- Interactive Leaflet map interface
- Background polling with caching
- Israeli flight detection
- Render deployment support