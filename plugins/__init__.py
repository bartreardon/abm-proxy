"""MDM Plugin system for ABM Proxy.

Drop a <name>.py + <name>.env pair into this directory to add a new plugin.
The server auto-discovers all subclasses of MDMPlugin at startup.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger('abm-proxy.plugins')


class MDMPlugin:
    """Base class for all MDM integration plugins.

    Subclass this, implement is_configured() and fetch(), and place
    the module in the plugins/ directory alongside a matching .env file.
    The .env file must share the same filename stem as the module
    (e.g. jamf.py reads jamf.env, jamf_site2.py reads jamf_site2.env).
    """

    #: Machine-readable key — set automatically from the filename by the loader.
    name: str = ''

    #: Human-readable label shown in the UI card header.
    display_name: str = ''

    #: The ABM MDM server UUID this plugin handles.
    #: Populated from ABM_SERVER_ID in the plugin's .env.
    #: Leave empty to match every device regardless of assigned server.
    abm_server_id: str = ''

    #: How long (in minutes) to cache plugin results.
    #: Short window — primarily protects against rate limiting.
    cache_ttl_minutes: int = 2

    #: Ordered list of fields to display in the UI card.
    #: Each entry: {"label": "Human Label", "key": "data_dict_key"}
    fields: list[dict] = []

    def is_configured(self) -> bool:
        """Return True if all required config values are present."""
        raise NotImplementedError

    def matches_device(self, abm_data: dict) -> bool:
        """Return True if this plugin should be queried for this device.

        Default: compare abm_server_id to abm_data['assignedServer']['id'].
        If abm_server_id is empty, match all devices.
        Override for custom matching logic.
        """
        if not self.abm_server_id:
            return True
        assigned = (abm_data or {}).get('assignedServer') or {}
        return assigned.get('id', '') == self.abm_server_id

    def fetch(self, serial: str, abm_data: dict | None = None) -> dict | None:
        """Query the MDM for this device and return a flat field dict, or None.

        Args:
            serial:   The device serial number (already upper-cased).
            abm_data: Full ABM device record — use it to branch on device type
                      (e.g. computer vs mobile device) if needed.

        Returns:
            A dict whose keys match the 'key' values in self.fields,
            or None if the device was not found in this MDM.
        """
        raise NotImplementedError


def load_plugins(plugins_dir: Path) -> list[MDMPlugin]:
    """Discover, instantiate, and return all configured plugin instances."""
    import importlib

    active: list[MDMPlugin] = []

    if not plugins_dir.is_dir():
        log.debug("plugins/ directory not found — no plugins loaded")
        return active

    for path in sorted(plugins_dir.glob('*.py')):
        module_name = path.stem
        if module_name.startswith('_'):
            continue

        try:
            module = importlib.import_module(f'plugins.{module_name}')
        except Exception as exc:
            log.warning("Could not import plugin module '%s': %s", module_name, exc)
            continue

        for attr_name in dir(module):
            cls = getattr(module, attr_name)
            if not (
                isinstance(cls, type)
                and issubclass(cls, MDMPlugin)
                and cls is not MDMPlugin
                and cls.__module__ == f'plugins.{module_name}'
            ):
                continue

            try:
                instance = cls()
                instance.name = module_name
                if instance.is_configured():
                    active.append(instance)
                    log.info(
                        "Plugin '%s' loaded (%s)",
                        module_name, instance.display_name or cls.__name__,
                    )
                else:
                    log.debug("Plugin '%s' not configured — skipped", module_name)
            except Exception as exc:
                log.warning("Could not initialise plugin '%s': %s", module_name, exc)

    return active
