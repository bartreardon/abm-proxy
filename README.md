# ABM Proxy

A lightweight caching proxy/middleware for the **Apple Business Manager (ABM)** and **Apple School Manager (ASM)** APIs.
Exposes a simple REST API and a built-in web UI for device lookup, with optional [SOFA](https://sofa.macadmins.io) integration for macOS release and compatibility information.

**Features**

- Single-device lookup with AppleCare coverage and MDM server details
- File-based device cache with configurable TTL and manual per-device refresh
- Background bulk-fetch of all ABM/ASM devices with automatic rate-limit handling
- Deep-linkable device results — share or bookmark `https://your-host/SERIALNUMBER`
- SOFA macOS version feed with disk caching — shows latest releases and per-device supported OS versions
- Generic API proxy — forward any ABM/ASM API call through the proxy without implementing each endpoint individually
- Supports both Apple Business Manager and Apple School Manager via configurable base URL and OAuth scope
- Built-in browser UI (no external dependencies)
- Optional API key protection for all `/api/*` endpoints; write operations via the proxy always require a key
- Docker-ready with configurable port

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Or use Docker — no Python install needed |
| `openssl` CLI | Used for ES256 JWT signing; pre-installed on macOS and most Linux distros |
| ABM Administrator access | Required to create API credentials |

---

## Obtaining API Credentials

You need four values from Apple Business Manager or Apple School Manager.

### 1 – Create an API key

**Apple Business Manager:** Sign in to [business.apple.com](https://business.apple.com) as an Administrator.
**Apple School Manager:** Sign in to [school.apple.com](https://school.apple.com) as an Administrator.

1. Go to **Settings → API & Privacy → API Keys**.
3. Click **Generate API Key**.
4. Enter a name, select the **Device Management** permission scope, and save.
5. Download the **private key** (`.p8` file) — Apple shows it only once.
   Store it securely (e.g. `private_key.pem`). Do not commit it to version control.

### 2 – Note the credential values

| ABM portal field | `.env` variable |
|---|---|
| Client ID | `ABM_CLIENT_ID` |
| Team ID (your organisation ID) | `ABM_TEAM_ID` |
| Key ID | `ABM_KEY_ID` |
| Downloaded private key file | `ABM_PRIVATE_KEY_FILE` |

> The `.p8` file Apple provides is already PEM-formatted. Rename it to `private_key.pem`
> (or any name you prefer) and reference that path in `ABM_PRIVATE_KEY_FILE`.
>
> Also ensure the file is in the correct format. The header should be `-----BEGIN PRIVATE KEY-----` <br>
> If it begins `-----BEGIN EC PRIVATE KEY-----`, remove the `EC` from the header and footer before using.

---

## Installation — Linux (manual)

### 1 – Get the files

```bash
git clone <repo-url>
cd abm-proxy
```

Or copy the `abm-proxy/` directory to your server.

### 2 – Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3 – Configure

```bash
cp .env.example .env
nano .env
```

Minimum required values:

```ini
ABM_CLIENT_ID=your-client-id
ABM_TEAM_ID=your-team-id
ABM_KEY_ID=your-key-id
ABM_PRIVATE_KEY_FILE=/absolute/path/to/private_key.pem
```

Set `PORT` to whichever port you want the service to listen on (default `5050`).

### 4 – Run

**Development / testing:**

```bash
python3 server.py
```

**Production (gunicorn):**

```bash
source .venv/bin/activate
gunicorn --bind "0.0.0.0:$(grep ^PORT .env | cut -d= -f2)" --workers 2 --timeout 60 server:app
```

Or simply let the `PORT` variable from `.env` flow through:

```bash
export $(grep -v '^#' .env | xargs)
gunicorn --bind "0.0.0.0:${PORT:-5050}" --workers 2 --timeout 60 server:app
```

Open `http://localhost:<PORT>` in your browser.

### 5 – Run as a systemd service (optional)

Create `/etc/systemd/system/abm-proxy.service`:

```ini
[Unit]
Description=ABM Proxy
After=network.target

[Service]
User=abm-proxy
WorkingDirectory=/opt/abm-proxy
EnvironmentFile=/opt/abm-proxy/.env
ExecStart=/opt/abm-proxy/.venv/bin/gunicorn \
    --bind 0.0.0.0:${PORT:-5050} --workers 2 --timeout 60 server:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now abm-proxy
sudo systemctl status abm-proxy
```

---

## Installation — Docker

### Before you start

Ensure the Docker daemon can reach the internet for the image pull.
On some Linux systems the default DNS is unreachable from inside Docker.
If the build fails with a DNS timeout, add this to `/etc/docker/daemon.json` and restart Docker:

```json
{
  "dns": ["8.8.8.8", "8.8.4.4"]
}
```

```bash
sudo systemctl restart docker
```

### 1 – Prepare configuration

```bash
cp .env.example .env
# Edit .env — fill in ABM credentials and set PORT if 5050 is already in use
nano .env
```

Copy your ABM private key to the same directory as `docker-compose.yml`:

```bash
cp /path/to/downloaded.p8 ./private_key.pem
chmod 600 ./private_key.pem
```

> The key is mounted read-only into the container at `/secrets/private_key.pem`.
> `ABM_PRIVATE_KEY_FILE` inside the container is always `/secrets/private_key.pem`
> regardless of where you store the file on the host.

### 2 – Build and start

```bash
sudo docker-compose up -d --build
```

The port exposed on the host is read from `PORT` in your `.env` (default `5050`).
Open `http://localhost:<PORT>` in your browser.

### Useful commands

```bash
# View logs (follow)
sudo docker-compose logs -f

# Stop the service
sudo docker-compose down

# Rebuild after editing server.py or static files
sudo docker-compose up -d --build

# Restart without rebuilding
sudo docker-compose restart

# Open a shell inside the running container
sudo docker-compose exec abm-proxy sh

# Check the cache directory inside the container
sudo docker-compose exec abm-proxy ls /data/cache/devices/
```

### Changing the port

Set `PORT` in `.env` — both the gunicorn bind address and the Docker port mapping
are driven by this variable:

```ini
# .env
PORT=6066
```

Then rebuild and restart:

```bash
sudo docker-compose down
sudo docker-compose up -d --build
```

### Persisting the cache across container restarts

Device cache files and the SOFA feed cache are stored in the named Docker volume
`abm-proxy_abm-cache`, which persists when you `down` and re-`up` the service.
ABM tokens are held in memory only and are re-acquired automatically after a restart.

---

## Configuration Reference

All settings are read from `.env` (or from environment variables directly).

| Variable | Default | Description |
|---|---|---|
| `ABM_CLIENT_ID` | — | OAuth2 Client ID (**required**) |
| `ABM_TEAM_ID` | — | Organisation / Team ID (**required**) |
| `ABM_KEY_ID` | — | API Key ID (**required**) |
| `ABM_PRIVATE_KEY_FILE` | — | Path to EC private key PEM file (**required**) |
| `ABM_API_BASE` | `https://api-business.apple.com/v1` | API base URL — change to `https://api-school.apple.com/v1` for ASM |
| `ABM_OAUTH_SCOPE` | `business.api` | OAuth2 scope — use `school.api` for ASM |
| `PORT` | `5050` | Port to listen on |
| `HOST` | `0.0.0.0` | Bind address |
| `CACHE_DIR` | `./cache` | Directory for cached device and SOFA JSON files |
| `CACHE_TTL_HOURS` | `24` | Device cache TTL in hours; `0` = never expire |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `API_KEY` | *(empty)* | When set, all `/api/*` endpoints require `X-API-Key: <value>` header; **must** be set to use write operations on `/v1/proxy/*` |
| `SOFA_ENABLED` | `true` | Enable SOFA macOS version and compatibility feed |
| `SOFA_FEED_URL` | *(SOFA default)* | Override the SOFA feed URL |
| `SOFA_CACHE_TTL_HOURS` | `6` | SOFA feed cache TTL in hours |

---

## Web UI

Open the service root in a browser to access the device lookup UI.

- Enter a serial number and press **Enter** or **Look Up**
- The URL updates to `/<SERIAL>` — results are bookmarkable and shareable as direct links
- Navigate directly to `http://your-host:PORT/C02XXXXXXMD6T` to pre-load a device
- **Refresh** fetches fresh data from ABM, bypassing the local cache
- Device cards show: hardware details, identifiers, enrolment status, purchase info,
  AppleCare coverage, and (for Macs) SOFA compatibility data including marketing name
  and supported OS versions
- The footer shows the latest macOS releases from the SOFA feed in descending version order

---

## API Reference

All endpoints return `application/json`.
When `API_KEY` is set, include `X-API-Key: <key>` in request headers,
or pass `?api_key=<key>` as a query parameter.

### `GET /health`

Service status — does not require an API key.

```json
{
  "status": "ok",
  "abm_enabled": true,
  "sofa_enabled": true,
  "cache_dir": "./cache",
  "cache_ttl_hours": 24,
  "cached_devices": 142
}
```

---

### `GET /api/v1/devices/<serial>`

Return device info from cache (if fresh) or live from ABM.

**Response (200):**
```json
{
  "serial_number": "C02XXXXXXMD6T",
  "device": {
    "attributes": {
      "serialNumber": "C02XXXXXXMD6T",
      "deviceModel": "MacBook Air (13-inch, M4, 2025)",
      "productType": "Mac16,12",
      "productFamily": "Mac",
      "status": "ASSIGNED",
      "color": "Sky Blue",
      "capacity": "256 GB",
      "wifiMacAddress": "AA:BB:CC:DD:EE:FF",
      "orderDateTime": "2025-03-01T00:00:00Z",
      ...
    }
  },
  "appleCareCoverage": [
    {
      "attributes": {
        "description": "AppleCare for Enterprise",
        "status": "ACTIVE",
        "startDateTime": "2025-04-25T00:00:00Z",
        "endDateTime": "2028-04-24T00:00:00Z",
        "agreementNumber": "ABC12345678",
        "isCanceled": false
      }
    }
  ],
  "assignedServer": {
    "attributes": { "serverName": "Acme MDM", "serverType": "MDM" }
  },
  "sofaModelInfo": {
    "MarketingName": "MacBook Air (13-inch, M4, 2025)",
    "SupportedOS": ["Tahoe 26", "Sequoia 15"],
    "OSVersions": [26, 15]
  },
  "cached_at": "2025-03-12T10:30:00+00:00",
  "from_cache": true
}
```

`sofaModelInfo` is `null` when SOFA is disabled or the `productType` is not in the SOFA
Models index (e.g. iPhones and iPads are not currently included in the SOFA feed).

**Error responses:**
- `404` – Serial not found in ABM
- `500` – ABM API error
- `501` – ABM not configured

---

### `POST /api/v1/devices/<serial>/refresh`

Delete the cached entry and fetch fresh data from ABM.

**Response (200):** same structure as the GET, with `"refreshed": true` and `"from_cache": false`.

---

### `GET /api/v1/devices`

List all device records currently in the local cache.

```json
{
  "total": 3,
  "devices": [ { ... }, { ... }, { ... } ]
}
```

---

### `POST /api/v1/devices/fetch`

Start a background job that paginates through all devices in ABM and writes them to
the local cache. Useful for pre-warming the cache or syncing a full fleet inventory.

**Request body (optional):**
```json
{ "fetch_warranty": true }
```

Setting `fetch_warranty: true` fetches AppleCare coverage for every device.
On large fleets this significantly increases run time and API call volume.
A 0.5 s delay is inserted between warranty calls to avoid rate limiting.

**Response (202):**
```json
{ "status": "started", "state": { ... } }
```

Returns `409` if a fetch is already in progress.

---

### `GET /api/v1/devices/fetch/status`

Poll the state of the running or most recently completed bulk fetch.

```json
{
  "status": "running",
  "message": "Processing 250/1200…",
  "started_at": "2025-03-12T10:00:00+00:00",
  "completed_at": null,
  "total": 1200,
  "processed": 250,
  "errors": 0,
  "running": true
}
```

`status` values: `idle` | `running` | `completed` | `failed`

---

### `GET /api/v1/sofa`

Return the latest macOS version data from the SOFA feed.
Add `?refresh=true` to force a network refresh regardless of cache age.

```json
{
  "sofa_enabled": true,
  "feed_url": "https://sofa.macadmins.io/v2/macos_data_feed.json",
  "cache_ttl_hours": 6,
  "versions": {
    "macOS Sequoia 15": {
      "version": "15.3.2",
      "build": "24D81",
      "release_date": "2025-03-12",
      "security_info": "...",
      "details_url": "https://support.apple.com/..."
    },
    "macOS Sonoma 14": { ... }
  }
}
```

---

### `GET|POST|PUT|PATCH|DELETE /v1/proxy/<path>`

Transparent passthrough to any ABM/ASM API endpoint. The proxy prepends `ABM_API_BASE`
and forwards query parameters, request body, and `Content-Type` as-is, returning the
upstream HTTP status code.

| Condition | Behaviour |
|---|---|
| `GET` | Follows normal `API_KEY` rules (optional if not configured) |
| `POST` / `PUT` / `PATCH` / `DELETE` | **Requires** `API_KEY` to be set and a valid key to be supplied — returns `403` if `API_KEY` is empty |

**Examples:**

```
GET  /v1/proxy/mdmServers
GET  /v1/proxy/mdmServers/{id}/relationships/devices
GET  /v1/proxy/orgDevices?limit=50&cursor=abc
```

These map to the equivalent paths under `ABM_API_BASE`, e.g.:
```
https://api-business.apple.com/v1/mdmServers
https://api-school.apple.com/v1/mdmServers/{id}/relationships/devices
```

**Error responses:**
- `403` – Write method attempted but `API_KEY` is not configured
- `401` – API key provided but incorrect
- `501` – ABM/ASM not configured
- `502` – Could not obtain access token or upstream request failed

---

## Caching Details

| Data | Storage | Default TTL |
|---|---|---|
| ABM JWT client assertion | In-memory | ~179 days |
| ABM OAuth2 access token | In-memory | ~55 minutes |
| Device records | `CACHE_DIR/devices/<SERIAL>.json` | `CACHE_TTL_HOURS` (default 24 h) |
| SOFA feed | In-memory + `CACHE_DIR/sofa_feed.json` | `SOFA_CACHE_TTL_HOURS` (default 6 h) |

Tokens are never written to disk. The SOFA feed is written to disk so it survives
service restarts without an immediate network fetch. Device cache files are plain JSON
and can be inspected or deleted manually at any time.

---

## HTTPS / TLS Setup

ABM Proxy speaks plain HTTP — use a reverse proxy for TLS termination.  
See [docs/https-setup.md](docs/https-setup.md) for details covering
Nginx + Certbot, Caddy, and self-signed certificates.

---

## Security Notes

- Restrict permissions on `private_key.pem`: `chmod 600 private_key.pem`
- Set `API_KEY` for any internet-facing deployment; this is **mandatory** to use write operations (`POST`/`PUT`/`PATCH`/`DELETE`) on `/v1/proxy/*`
- The cache directory contains device data from your ABM organisation — secure it accordingly
- Do not commit `.env` or `private_key.pem` to version control
  (add both to `.gitignore`)
