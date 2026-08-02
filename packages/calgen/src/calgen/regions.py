import os
import importlib.util


class EventRejected(Exception):
    """Raised by location_to_region() to signal the event should be excluded."""


def load_region_plugin(site_dir):
    """Load {site_dir}/regions.py as a plugin module, or return None."""
    plugin_path = os.path.join(site_dir, 'regions.py')
    if not os.path.exists(plugin_path):
        return None
    spec = importlib.util.spec_from_file_location('regions', plugin_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
