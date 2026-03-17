"""FleetDM MDM plugin for ABM Proxy.

Queries the FleetDM REST API for host records using the device serial number.
Authentication uses a static API token generated from the Fleet UI
(My account → Get API token, or via /profile).

Copy fleetdm.env.example to fleetdm.env and fill in your values.

API references:
  Host by identifier : GET /api/v1/fleet/hosts/identifier/{identifier}
  Auth               : Authorization: Bearer <api_token>  (static token, no OAuth)
"""
from __future__ import annotations

import logging
from pathlib import Path

import requests
from dotenv import dotenv_values

from plugins import MDMPlugin

log = logging.getLogger('abm-proxy.plugins.fleetdm')

_PLUGIN_DIR = Path(__file__).parent


class FleetDMPlugin(MDMPlugin):

    display_name      = 'FleetDM'
    cache_ttl_minutes = 2

    fields = [
        {'label': 'Status',         'key': 'status'},
        {'label': 'Last Seen',      'key': 'last_seen'},
        {'label': 'OS Version',     'key': 'os_version'},
        {'label': 'Device Name',    'key': 'device_name'},
        {'label': 'Team',           'key': 'team'},
        {'label': 'Assigned User',  'key': 'assigned_user'},
        {'label': 'MDM Enrollment', 'key': 'mdm_enrollment'},
    ]

    def __init__(self) -> None:
        cfg = dotenv_values(_PLUGIN_DIR / f'{Path(__file__).stem}.env')

        self.display_name       = cfg.get('DISPLAY_NAME', 'FleetDM')
        self.abm_server_id      = cfg.get('ABM_SERVER_ID', '')
        self.cache_ttl_minutes  = int(cfg.get('CACHE_TTL_MINUTES', '2'))
        self._url               = cfg.get('URL', '').rstrip('/')
        self._api_token         = cfg.get('API_TOKEN', '')

    def is_configured(self) -> bool:
        return bool(self._url and self._api_token)

    # ------------------------------------------------------------------

    def fetch(self, serial: str, abm_data: dict | None = None) -> dict | None:
        """Look up a host by serial number using the identifier endpoint."""
        resp = requests.get(
            f'{self._url}/api/v1/fleet/hosts/identifier/{serial}',
            headers={
                'Authorization': f'Bearer {self._api_token}',
                'Accept':        'application/json',
            },
            timeout=15,
        )

        if resp.status_code == 404:
            return None
        resp.raise_for_status()

        host = resp.json().get('host', {})
        mdm  = host.get('mdm', {})

        return {
            'status':         host.get('status'),
            'last_seen':      host.get('seen_time'),
            'os_version':     host.get('os_version'),
            'device_name':    host.get('computer_name') or host.get('hostname'),
            'team':           host.get('team_name'),
            'assigned_user':  host.get('primary_username'),
            'mdm_enrollment': mdm.get('enrollment_status'),
        }
