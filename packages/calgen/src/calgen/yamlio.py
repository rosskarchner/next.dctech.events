"""YAML loading for the build path.

PyYAML's pure-Python SafeLoader is ~10x slower than libyaml's CSafeLoader, and
the frozen build re-reads the group/category/archive/post files on most
requests. CSafeLoader is available wherever PyYAML ships its compiled wheels;
fall back to the pure-Python loader where it is not, so behaviour is the same
either way.
"""
import yaml

_Loader = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)

# Name of the loader in use, for the build log: a fallback to the pure-Python
# SafeLoader is correct but ~10x slower, and nothing else would say so.
LOADER_NAME = _Loader.__name__


def safe_load(stream):
    return yaml.load(stream, Loader=_Loader)  # nosec B506 - safe loader
