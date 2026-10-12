"""Make the calgen modules build_lambdas.sh copies into the MCP Lambda
importable by their flat names, as they are in the deployed bundle.

Loaded by file path rather than by putting packages/calgen/src/calgen on
sys.path: that directory also holds modules (event_utils, ...) that would
shadow this Lambda's own copies.
"""
import importlib.util
import os
import sys

_CALGEN = os.path.join(os.path.dirname(__file__), '..', '..', '..',
                       'packages', 'calgen', 'src', 'calgen')

for _name in ('json_ld', 'ssrf_guard'):
    if _name not in sys.modules:
        _spec = importlib.util.spec_from_file_location(
            _name, os.path.join(_CALGEN, f'{_name}.py'))
        _module = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_module)
        sys.modules[_name] = _module
