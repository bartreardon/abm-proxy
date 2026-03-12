#!/usr/bin/env python3
"""
ABM Proxy - Apple Business Manager API Middleware
Standalone proxy/caching service for the ABM device management API.
"""

import os
import json
import time
import uuid
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps

import jwt
import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ABM_CLIENT_ID       = os.getenv('ABM_CLIENT_ID', '')
ABM_TEAM_ID         = os.getenv('ABM_TEAM_ID', '')
ABM_KEY_ID          = os.getenv('ABM_KEY_ID', '')
ABM_PRIVATE_KEY_FILE = os.getenv('ABM_PRIVATE_KEY_FILE', '')

CACHE_DIR           = os.getenv('CACHE_DIR', './cache')
CACHE_TTL_HOURS     = float(os.getenv('CACHE_TTL_HOURS', '24'))   # 0 = never expire
LOG_LEVEL           = os.getenv('LOG_LEVEL', 'INFO')
HOST                = os.getenv('HOST', '0.0.0.0')
PORT                = int(os.getenv('PORT', '5050'))
API_KEY             = os.getenv('API_KEY', '')                     # optional auth

SOFA_ENABLED        = os.getenv('SOFA_ENABLED', 'true').lower() == 'true'
SOFA_FEED_URL       = os.getenv('SOFA_FEED_URL',
                        'https://sofafeed.macadmins.io/v1/macos_data_feed.json')
SOFA_CACHE_TTL_HOURS = float(os.getenv('SOFA_CACHE_TTL_HOURS', '6'))

ABM_ENABLED = all([ABM_CLIENT_ID, ABM_KEY_ID, ABM_PRIVATE_KEY_FILE])
ABM_API_BASE = 'https://api-business.apple.com/v1'
ABM_AUTH_URL = 'https://account.apple.com/auth/oauth2/token'

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
log = logging.getLogger('abm-proxy')

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder='static')
CORS(app)

# ---------------------------------------------------------------------------
# In-memory token caches
# ---------------------------------------------------------------------------
_assertion_cache: dict = {'value': None, 'expires': 0}
_token_cache: dict     = {'value': None, 'expires': 0}
_sofa_cache: dict      = {'data': None,  'expires': 0}

# ---------------------------------------------------------------------------
# Bulk fetch state
# ---------------------------------------------------------------------------
_bulk_state: dict = {
    'status':       'idle',   # idle | running | completed | failed
    'message':      '',
    'started_at':   None,
    'completed_at': None,
    'total':        0,
    'processed':    0,
    'errors':       0,
    'running':      False,
}
_bulk_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Ensure cache directories exist
# ---------------------------------------------------------------------------
Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
Path(f'{CACHE_DIR}/devices').mkdir(parents=True, exist_ok=True)


# ===========================================================================
# Authentication
# ===========================================================================

def generate_client_assertion() -> str:
    """Build and sign an ES256 JWT for use as an OAuth2 client_assertion."""
    now = int(time.time())
    if _assertion_cache['value'] and _assertion_cache['expires'] > now:
        return _assertion_cache['value']

    key_path = Path(ABM_PRIVATE_KEY_FILE)
    if not key_path.exists():
        raise RuntimeError(f"Private key file not found: {ABM_PRIVATE_KEY_FILE}")

    private_key = key_path.read_text()
    payload = {
        'sub': ABM_CLIENT_ID,
        'iss': ABM_CLIENT_ID,
        'aud': 'https://account.apple.com/auth/oauth2/v2/token',
        'iat': now,
        'exp': now + 180 * 86400,
        'jti': str(uuid.uuid4()),
    }
    assertion = jwt.encode(
        payload,
        private_key,
        algorithm='ES256',
        headers={'kid': ABM_KEY_ID},
    )
    _assertion_cache.update({'value': assertion, 'expires': now + 179 * 86400})
    log.info("Generated new ABM client assertion (valid ~179 days)")
    return assertion


def get_token(force_refresh: bool = False) -> str:
    """Exchange client assertion for an ABM OAuth2 access token."""
    now = int(time.time())
    if not force_refresh and _token_cache['value'] and _token_cache['expires'] > now:
        return _token_cache['value']

    assertion = generate_client_assertion()
    resp = requests.post(
        ABM_AUTH_URL,
        data={
            'grant_type': 'client_credentials',
            'client_id': ABM_CLIENT_ID,
            'client_assertion_type':
                'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
            'client_assertion': assertion,
            'scope': 'business.api',
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()['access_token']
    _token_cache.update({'value': token, 'expires': now + 3300})  # 55 min
    log.info("Obtained new ABM access token")
    return token


# ===========================================================================
# ABM API helpers
# ===========================================================================

def _abm_get(path: str, token: str | None = None, params: dict | None = None):
    """Authenticated GET against the ABM API."""
    if token is None:
        token = get_token()
    return requests.get(
        f'{ABM_API_BASE}{path}',
        params=params,
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
        timeout=30,
    )


def fetch_device_from_abm(serial: str, token: str | None = None) -> dict:
    """Fetch a single device record (+ coverage + MDM server) from ABM."""
    serial = serial.upper()
    if token is None:
        token = get_token()

    resp = _abm_get(f'/orgDevices/{serial}', token)
    if resp.status_code == 404:
        return {'error': 'not_found', 'serial_number': serial}
    resp.raise_for_status()

    device = resp.json().get('data', {})
    result: dict = {
        'serial_number': serial,
        'device': device,
        'appleCareCoverage': None,
        'assignedServer': None,
    }

    # AppleCare coverage (optional - non-fatal)
    try:
        cov = _abm_get(f'/orgDevices/{serial}/appleCareCoverage', token)
        if cov.ok:
            result['appleCareCoverage'] = cov.json().get('data', [])
    except Exception as e:
        log.warning("Could not fetch AppleCare coverage for %s: %s", serial, e)

    # MDM server (only relevant when ASSIGNED)
    if device.get('attributes', {}).get('status') == 'ASSIGNED':
        try:
            srv = _abm_get(f'/orgDevices/{serial}/assignedServer', token)
            if srv.ok:
                result['assignedServer'] = srv.json().get('data', {})
        except Exception as e:
            log.warning("Could not fetch assigned server for %s: %s", serial, e)

    return result


# ===========================================================================
# Device cache
# ===========================================================================

def _cache_path(serial: str) -> Path:
    return Path(CACHE_DIR) / 'devices' / f'{serial.upper()}.json'


def read_cache(serial: str) -> dict | None:
    path = _cache_path(serial)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if CACHE_TTL_HOURS > 0:
            cached_at = data.get('cached_at')
            if cached_at:
                age = (
                    datetime.now(timezone.utc) -
                    datetime.fromisoformat(cached_at)
                ).total_seconds()
                if age > CACHE_TTL_HOURS * 3600:
                    return None
        return data
    except Exception as e:
        log.warning("Cache read error for %s: %s", serial, e)
        return None


def write_cache(serial: str, data: dict) -> None:
    data['cached_at'] = datetime.now(timezone.utc).isoformat()
    _cache_path(serial).write_text(json.dumps(data, indent=2))


def delete_cache(serial: str) -> bool:
    path = _cache_path(serial)
    if path.exists():
        path.unlink()
        return True
    return False


def list_cached_devices() -> list[dict]:
    out = []
    for f in (Path(CACHE_DIR) / 'devices').glob('*.json'):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            pass
    return out


def _enrich_with_sofa(data: dict) -> dict:
    """Attach SOFA model info to a device record (mutates in place, returns it)."""
    if not SOFA_ENABLED or data.get('error'):
        return data
    product_type = data.get('device', {}).get('attributes', {}).get('productType')
    data['sofaModelInfo'] = sofa_model_info(product_type) if product_type else None
    return data


def get_device_info(serial: str, force_refresh: bool = False) -> tuple[dict | None, bool]:
    """Return (data, from_cache). Fetches from ABM if needed."""
    serial = serial.upper().strip()

    if not force_refresh:
        cached = read_cache(serial)
        if cached:
            # Always re-run SOFA enrichment so it reflects current SOFA data
            # even when the device record itself is served from cache.
            return _enrich_with_sofa(cached), True

    if not ABM_ENABLED:
        return None, False

    try:
        data = fetch_device_from_abm(serial)
        write_cache(serial, data)
        return _enrich_with_sofa(data), False
    except Exception as e:
        log.error("Error fetching %s from ABM: %s", serial, e)
        return None, False


# ===========================================================================
# SOFA integration
# ===========================================================================

_SOFA_DISK_CACHE = Path(CACHE_DIR) / 'sofa_feed.json'


def _sofa_disk_read() -> dict | None:
    """Return disk-cached SOFA data if it is still within TTL."""
    if not _SOFA_DISK_CACHE.exists():
        return None
    try:
        raw = json.loads(_SOFA_DISK_CACHE.read_text())
        saved_at = raw.get('_cached_at', 0)
        if time.time() - saved_at < SOFA_CACHE_TTL_HOURS * 3600:
            return raw.get('data')
    except Exception:
        pass
    return None


def _sofa_disk_write(data: dict) -> None:
    try:
        _SOFA_DISK_CACHE.write_text(
            json.dumps({'_cached_at': time.time(), 'data': data}, indent=2)
        )
    except Exception as e:
        log.warning("Could not write SOFA disk cache: %s", e)


def get_sofa_data(force_refresh: bool = False) -> dict | None:
    if not SOFA_ENABLED:
        return None
    now = time.time()

    # 1 – in-memory (fastest)
    if not force_refresh and _sofa_cache['data'] and _sofa_cache['expires'] > now:
        return _sofa_cache['data']

    # 2 – disk cache (survives restarts)
    if not force_refresh:
        disk = _sofa_disk_read()
        if disk:
            _sofa_cache.update({'data': disk, 'expires': now + SOFA_CACHE_TTL_HOURS * 3600})
            log.debug("SOFA feed loaded from disk cache")
            return disk

    # 3 – fetch from network
    try:
        resp = requests.get(SOFA_FEED_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        _sofa_cache.update({'data': data, 'expires': now + SOFA_CACHE_TTL_HOURS * 3600})
        _sofa_disk_write(data)
        log.info("Refreshed SOFA feed from network")
    except Exception as e:
        log.warning("SOFA fetch failed: %s", e)

    return _sofa_cache.get('data')


def latest_macos_versions() -> dict:
    data = get_sofa_data()
    if not data:
        return {}
    result = {}
    try:
        for entry in data.get('OSVersions', []):
            name   = entry.get('OSVersion', '')
            latest = entry.get('Latest', {})
            result[name] = {
                'version':      latest.get('ProductVersion', ''),
                'build':        latest.get('Build', ''),
                'release_date': latest.get('ReleaseDate', ''),
                'security_info': latest.get('SecurityInfo', ''),
                'details_url':  latest.get('DetailsURL', ''),
            }
    except Exception as e:
        log.warning("Error parsing SOFA data: %s", e)
    return result


def sofa_model_info(product_type: str) -> dict | None:
    """Return SOFA Models entry for a given productType (e.g. 'Mac16,12')."""
    if not product_type or not SOFA_ENABLED:
        return None
    data = get_sofa_data()
    if not data:
        return None
    models = data.get('Models', {})
    return models.get(product_type)


# ===========================================================================
# Bulk fetch (background thread)
# ===========================================================================

def _bulk_fetch_worker(fetch_warranty: bool) -> None:
    global _bulk_state

    with _bulk_lock:
        if _bulk_state['running']:
            return
        _bulk_state.update({
            'running': True,
            'status': 'running',
            'started_at': datetime.now(timezone.utc).isoformat(),
            'completed_at': None,
            'total': 0, 'processed': 0, 'errors': 0,
            'message': 'Fetching device list from ABM…',
        })

    try:
        token   = get_token()
        devices = []
        cursor  = None

        # Paginate through all devices
        while True:
            params = {'limit': 200}
            if cursor:
                params['cursor'] = cursor

            resp = _abm_get('/orgDevices', token, params)

            if resp.status_code == 429:
                wait = int(resp.headers.get('Retry-After', 60))
                log.warning("Rate limited, waiting %ds", wait)
                _bulk_state['message'] = f'Rate limited by ABM – waiting {wait}s…'
                time.sleep(wait)
                continue

            resp.raise_for_status()
            page = resp.json()
            devices.extend(page.get('data', []))

            cursor = page.get('meta', {}).get('paging', {}).get('nextCursor')
            if not cursor:
                break

        _bulk_state['total'] = len(devices)
        log.info("Bulk fetch: %d devices retrieved", len(devices))

        for i, device in enumerate(devices):
            attrs  = device.get('attributes', {})
            serial = attrs.get('serialNumber', '').upper()
            if not serial:
                continue

            result: dict = {
                'serial_number': serial,
                'device': device,
                'appleCareCoverage': None,
                'assignedServer': None,
            }

            if fetch_warranty:
                for attempt in range(3):
                    try:
                        cov = _abm_get(f'/orgDevices/{serial}/appleCareCoverage', token)
                        if cov.status_code == 429:
                            wait = int(cov.headers.get('Retry-After', 60))
                            time.sleep(wait)
                            continue
                        if cov.ok:
                            result['appleCareCoverage'] = cov.json().get('data', [])
                        break
                    except Exception as e:
                        log.warning("Coverage fetch error %s: %s", serial, e)
                        break
                time.sleep(0.5)  # gentle rate limiting

            if attrs.get('status') == 'ASSIGNED':
                try:
                    srv = _abm_get(f'/orgDevices/{serial}/assignedServer', token)
                    if srv.ok:
                        result['assignedServer'] = srv.json().get('data', {})
                except Exception:
                    pass

            try:
                write_cache(serial, result)
            except Exception as e:
                log.warning("Cache write error %s: %s", serial, e)
                _bulk_state['errors'] += 1

            _bulk_state['processed'] = i + 1
            if (i + 1) % 100 == 0:
                _bulk_state['message'] = f'Processing {i+1}/{len(devices)}…'
                log.info("Bulk fetch progress: %d/%d", i + 1, len(devices))

        _bulk_state.update({
            'running': False,
            'status': 'completed',
            'completed_at': datetime.now(timezone.utc).isoformat(),
            'message': (
                f'Done – {_bulk_state["processed"]} devices processed, '
                f'{_bulk_state["errors"]} errors'
            ),
        })
        log.info("Bulk fetch complete")

    except Exception as e:
        log.error("Bulk fetch failed: %s", e)
        _bulk_state.update({
            'running': False,
            'status': 'failed',
            'completed_at': datetime.now(timezone.utc).isoformat(),
            'message': str(e),
        })


# ===========================================================================
# Optional API-key guard
# ===========================================================================

def require_api_key(f):
    @wraps(f)
    def _inner(*args, **kwargs):
        if API_KEY:
            provided = (
                request.headers.get('X-API-Key') or
                request.args.get('api_key', '')
            )
            if provided != API_KEY:
                return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return _inner


# ===========================================================================
# Routes
# ===========================================================================

@app.route('/', defaults={'serial_path': None})
@app.route('/<path:serial_path>')
def index(serial_path):
    del serial_path  # path segment is read by JS via window.location
    return send_from_directory('static', 'index.html')


@app.get('/health')
def health():
    cached_count = len(list((Path(CACHE_DIR) / 'devices').glob('*.json')))
    return jsonify({
        'status':          'ok',
        'abm_enabled':     ABM_ENABLED,
        'sofa_enabled':    SOFA_ENABLED,
        'cache_dir':       CACHE_DIR,
        'cache_ttl_hours': CACHE_TTL_HOURS,
        'cached_devices':  cached_count,
    })


# ---- Device endpoints -------------------------------------------------------

@app.get('/api/v1/devices/<serial_number>')
@require_api_key
def get_device(serial_number):
    """Return device info from cache or live ABM API."""
    if not ABM_ENABLED:
        return jsonify({'error': 'ABM not configured'}), 501

    data, from_cache = get_device_info(serial_number)
    if data is None:
        return jsonify({'error': 'Failed to retrieve device info'}), 500
    if data.get('error') == 'not_found':
        return jsonify({
            'error': 'Device not found in ABM',
            'serial_number': serial_number.upper(),
        }), 404

    return jsonify({**data, 'from_cache': from_cache})


@app.post('/api/v1/devices/<serial_number>/refresh')
@require_api_key
def refresh_device(serial_number):
    """Purge cache and fetch fresh data from ABM."""
    if not ABM_ENABLED:
        return jsonify({'error': 'ABM not configured'}), 501

    delete_cache(serial_number)
    data, _ = get_device_info(serial_number, force_refresh=True)
    if data is None:
        return jsonify({'error': 'Failed to refresh device info'}), 500
    if data.get('error') == 'not_found':
        return jsonify({
            'error': 'Device not found in ABM',
            'serial_number': serial_number.upper(),
        }), 404

    return jsonify({**data, 'refreshed': True, 'from_cache': False})


@app.get('/api/v1/devices')
@require_api_key
def list_devices():
    """Return all devices currently in the local cache."""
    devices = list_cached_devices()
    return jsonify({'total': len(devices), 'devices': devices})


# ---- Bulk fetch endpoints ---------------------------------------------------

@app.post('/api/v1/devices/fetch')
@require_api_key
def start_bulk_fetch():
    """Start a background job to pull all devices from ABM into the cache."""
    if not ABM_ENABLED:
        return jsonify({'error': 'ABM not configured'}), 501

    if _bulk_state['running']:
        return jsonify({'error': 'Bulk fetch already running', 'state': _bulk_state}), 409

    fetch_warranty = (
        request.get_json(silent=True) or {}
    ).get('fetch_warranty', False)

    t = threading.Thread(
        target=_bulk_fetch_worker, args=(fetch_warranty,), daemon=True
    )
    t.start()
    return jsonify({'status': 'started', 'state': _bulk_state}), 202


@app.get('/api/v1/devices/fetch/status')
@require_api_key
def bulk_fetch_status():
    return jsonify(_bulk_state)


# ---- SOFA endpoint ----------------------------------------------------------

@app.get('/api/v1/sofa')
def sofa():
    if not SOFA_ENABLED:
        return jsonify({'error': 'SOFA integration not enabled'}), 501

    force = request.args.get('refresh', '').lower() == 'true'
    if force:
        get_sofa_data(force_refresh=True)

    return jsonify({
        'sofa_enabled':  True,
        'versions':      latest_macos_versions(),
        'feed_url':      SOFA_FEED_URL,
        'cache_ttl_hours': SOFA_CACHE_TTL_HOURS,
    })


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == '__main__':
    log.info(
        "Starting ABM Proxy on %s:%s  (ABM: %s | SOFA: %s)",
        HOST, PORT, ABM_ENABLED, SOFA_ENABLED,
    )
    if not ABM_ENABLED:
        log.warning(
            "ABM is NOT configured – set ABM_CLIENT_ID, ABM_TEAM_ID, "
            "ABM_KEY_ID and ABM_PRIVATE_KEY_FILE in .env"
        )
    app.run(host=HOST, port=PORT, debug=False)
