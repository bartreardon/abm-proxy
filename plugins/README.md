# ABM Proxy – MDM Plugins

Plugins let you pull additional device data from any MDM or external API and surface it alongside the ABM record in the device lookup UI. Each plugin is a single Python file paired with a `.env` configuration file.

---

## How it works

At startup the server scans this directory for `*.py` files, imports each one, and instantiates any class that subclasses `MDMPlugin`. A plugin is activated only if its `is_configured()` method returns `True` (i.e. the required `.env` values are present).

When a device is looked up:
1. The ABM record is resolved (from cache or live).
2. Each active plugin whose `matches_device()` passes is queried **in parallel**.
3. Results are merged into the response under `mdmPlugins.<plugin_name>` and rendered as a card in the web UI.

Plugin results are cached separately from ABM data with a short TTL (default 2 minutes) to protect against rate limiting. Errors are never cached — a failed call is retried on the next request.

---

## Included plugins

| File | MDM | Notes |
|------|-----|-------|
| `jamf.py` | Jamf Pro | Computers (v3 API) and mobile devices (v2 API); OAuth2 client credentials |
| `fleetdm.py` | FleetDM | All platforms; static API token auth |

---

## Installing a plugin

1. Copy the `.py` file into this directory.
2. Copy or create the matching `.env` file (same filename stem, e.g. `jamf.py` → `jamf.env`).
3. Fill in the required values — see `jamf.env.example` for a documented template.
4. Restart the server. The plugin appears in startup logs and in `/health`.

### Multiple instances of the same MDM

To connect two Jamf Pro servers, duplicate both files with a suffix:

```
plugins/
  jamf.py          jamf.env          ← site 1
  jamf_site2.py    jamf_site2.env    ← site 2
```

Each instance loads its own `.env` and can be scoped to a specific ABM MDM server via `ABM_SERVER_ID` so only the right plugin is queried for each device.

### Finding your ABM server ID

```
GET /api/v1/proxy/mdmServers
```

The `id` field on each result (e.g. `1F97349736CF4614A94F624E705841AD`) is what you set as `ABM_SERVER_ID` in the plugin's `.env`. When set, the plugin is skipped for devices not assigned to that server.

---

## Writing a custom plugin

Create `plugins/myplugin.py`:

```python
"""My MDM plugin."""
from __future__ import annotations

from pathlib import Path
from dotenv import dotenv_values
import requests
from plugins import MDMPlugin

_PLUGIN_DIR = Path(__file__).parent


class MyMDMPlugin(MDMPlugin):

    display_name      = 'My MDM'
    cache_ttl_minutes = 2

    # Fields shown in the UI card, in display order.
    # 'key' must match a key returned by fetch().
    fields = [
        {'label': 'Managed',     'key': 'managed'},
        {'label': 'Last Seen',   'key': 'last_seen'},
        {'label': 'OS Version',  'key': 'os_version'},
        {'label': 'Device Name', 'key': 'device_name'},
    ]

    def __init__(self) -> None:
        # Load config from the .env that shares this file's stem name.
        cfg = dotenv_values(_PLUGIN_DIR / f'{Path(__file__).stem}.env')

        self.display_name       = cfg.get('DISPLAY_NAME', 'My MDM')
        self.abm_server_id      = cfg.get('ABM_SERVER_ID', '')
        self.cache_ttl_minutes  = int(cfg.get('CACHE_TTL_MINUTES', '2'))
        self._url               = cfg.get('URL', '').rstrip('/')
        self._api_key           = cfg.get('API_KEY', '')

    def is_configured(self) -> bool:
        """Return True only when all required config is present."""
        return bool(self._url and self._api_key)

    def fetch(self, serial: str, abm_data: dict | None = None) -> dict | None:
        """
        Query your MDM for the device and return a flat dict of field values,
        or None if the device is not found.

        'abm_data' contains the full ABM record — use it to branch on device
        type (productFamily: 'Mac', 'iPhone', 'iPad', etc.) if needed.
        """
        resp = requests.get(
            f'{self._url}/api/devices/{serial}',
            headers={'Authorization': f'Bearer {self._api_key}'},
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        d = resp.json()
        return {
            'managed':     d.get('isManaged'),
            'last_seen':   d.get('lastSeen'),
            'os_version':  d.get('osVersion'),
            'device_name': d.get('name'),
        }
```

Create the paired `plugins/myplugin.env`:

```ini
DISPLAY_NAME=My MDM
ABM_SERVER_ID=
URL=https://mdm.example.com
API_KEY=
CACHE_TTL_MINUTES=2
```

---

## Plugin API reference

### `MDMPlugin` — base class (`plugins/__init__.py`)

| Attribute / Method | Type | Description |
|---|---|---|
| `name` | `str` | Set automatically from the filename stem. Do not set manually. |
| `display_name` | `str` | Label shown in the UI card header. |
| `abm_server_id` | `str` | ABM MDM server UUID. Empty = match all devices. |
| `cache_ttl_minutes` | `int` | How long results are cached (default `2`). |
| `fields` | `list[dict]` | Ordered list of `{"label": "...", "key": "..."}` for the UI. |
| `is_configured()` | `bool` | Return `True` when all required config is available. |
| `matches_device(abm_data)` | `bool` | Override for custom device matching. Default matches on `abm_server_id`. |
| `fetch(serial, abm_data)` | `dict \| None` | **Required.** Query the MDM and return field values, or `None` if not found. |

### Response format

Each plugin's contribution in the API response (`mdmPlugins.<name>`):

```json
{
  "display_name": "Jamf Pro",
  "from_cache": true,
  "cached_at": "2026-03-17T10:01:23Z",
  "error": null,
  "fields": [
    {"label": "Managed", "key": "managed"},
    {"label": "Last Check-in", "key": "last_check_in"}
  ],
  "data": {
    "managed": true,
    "last_check_in": "2026-03-17T10:00:00Z"
  }
}
```

If the device is not found in the MDM, `data` is `null` and the UI shows "Not found in this MDM". If the call fails, `error` is set and the request is not cached.
