"""Jamf Pro MDM plugin for ABM Proxy.

Queries the Jamf Pro modern API (v1/v2) for computer and mobile device
records using the device serial number. Authentication uses OAuth2
client credentials (Settings → API roles and clients in Jamf Pro).

Copy jamf.env.example to jamf.env and fill in your values.
For a second Jamf instance, copy both files to jamf_site2.py / jamf_site2.env.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

from plugins import MDMPlugin

log = logging.getLogger('abm-proxy.plugins.jamf')

_PLUGIN_DIR = Path(__file__).parent


class JamfPlugin(MDMPlugin):

    display_name      = 'Jamf Pro'
    cache_ttl_minutes = 2

    fields = [
        {'label': 'Managed',       'key': 'managed'},
        {'label': 'Last Check-in', 'key': 'last_check_in'},
        {'label': 'OS Version',    'key': 'os_version'},
        {'label': 'Device Name',   'key': 'device_name'},
        {'label': 'Department',    'key': 'department'},
        {'label': 'Assigned User', 'key': 'assigned_user'},
    ]

    def __init__(self) -> None:
        # Each plugin instance loads the .env that shares its module filename stem.
        # jamf.py → jamf.env,  jamf_site2.py → jamf_site2.env, etc.
        env_file = _PLUGIN_DIR / f'{Path(__file__).stem}.env'
        cfg = dotenv_values(env_file)

        self.display_name       = cfg.get('DISPLAY_NAME', 'Jamf Pro')
        self.abm_server_id      = cfg.get('ABM_SERVER_ID', '')
        self.cache_ttl_minutes  = int(cfg.get('CACHE_TTL_MINUTES', '2'))
        self._url               = cfg.get('URL', '').rstrip('/')
        self._client_id         = cfg.get('CLIENT_ID', '')
        self._client_secret     = cfg.get('CLIENT_SECRET', '')
        self._token: str | None = None
        self._token_expires: float = 0.0

    def is_configured(self) -> bool:
        return bool(self._url and self._client_id and self._client_secret)

    # ------------------------------------------------------------------
    # Token management

    def _get_token(self) -> str:
        now = time.time()
        if self._token and self._token_expires > now:
            return self._token
        resp = requests.post(
            f'{self._url}/api/oauth/token',
            data={
                'grant_type':    'client_credentials',
                'client_id':     self._client_id,
                'client_secret': self._client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body['access_token']
        self._token_expires = now + int(body.get('expires_in', 1800)) - 60
        log.debug("Jamf token refreshed for '%s'", self.display_name)
        return self._token

    # ------------------------------------------------------------------
    # Device lookups

    def _fetch_computer(self, serial: str, token: str) -> dict | None:
        resp = requests.get(
            f'{self._url}/api/v1/computers-inventory',
            params={
                'filter':  f'hardware.serialNumber=="{serial}"',
                'section': ['GENERAL', 'USER_AND_LOCATION', 'OPERATING_SYSTEM'],
            },
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get('results', [])
        if not results:
            return None
        c       = results[0]
        general = c.get('general', {})
        loc     = c.get('userAndLocation', {})
        os_     = c.get('operatingSystem', {})
        return {
            'managed':       general.get('remoteManagement', {}).get('managed'),
            'last_check_in': general.get('lastContactTime'),
            'os_version':    os_.get('version'),
            'device_name':   general.get('name'),
            'department':    loc.get('department'),
            'assigned_user': loc.get('username'),
        }

    def _fetch_mobile(self, serial: str, token: str) -> dict | None:
        resp = requests.get(
            f'{self._url}/api/v2/mobile-devices',
            params={
                'filter':  f'serialNumber=="{serial}"',
                'section': ['GENERAL', 'USER_AND_LOCATION'],
            },
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get('results', [])
        if not results:
            return None
        m       = results[0]
        general = m.get('general', {})
        loc     = m.get('userAndLocation', {})
        return {
            'managed':       general.get('managed'),
            'last_check_in': general.get('lastInventoryUpdateDate'),
            'os_version':    general.get('osVersion'),
            'device_name':   general.get('displayName'),
            'department':    loc.get('department'),
            'assigned_user': loc.get('username'),
        }

    # ------------------------------------------------------------------

    def fetch(self, serial: str, abm_data: dict | None = None) -> dict | None:
        token  = self._get_token()
        family = (abm_data or {}).get('device', {}).get('attributes', {}).get('productFamily', '')

        if family in ('iPhone', 'iPad'):
            return self._fetch_mobile(serial, token)

        # Default: try computer endpoint; fall back to mobile if not found
        result = self._fetch_computer(serial, token)
        if result is None:
            result = self._fetch_mobile(serial, token)
        return result
