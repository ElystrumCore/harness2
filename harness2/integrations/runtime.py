"""Explicit, credential-free local runtime configuration for the optional lab."""
from __future__ import annotations

import json
from pathlib import Path

from .gateway import Refused

FIELDS = frozenset({'oc_root', 'bun', 'chuck_root', 'hub_root', 'chuck_python'})


def load_runtime(path):
    try:
        values = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise Refused('runtime configuration is missing or malformed') from exc
    if not isinstance(values, dict) or set(values) != FIELDS:
        raise Refused('runtime configuration must contain exactly the five documented paths')
    result = {}
    for name, value in values.items():
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise Refused('runtime paths must be explicit absolute paths')
        try:
            target = Path(value).resolve(strict=True)
        except OSError as exc:
            raise Refused('runtime path does not exist') from exc
        if name.endswith('_root') and not target.is_dir():
            raise Refused('runtime source root is not a directory')
        if not name.endswith('_root') and not target.is_file():
            raise Refused('runtime executable is not a file')
        # Preserve virtual-environment executable aliases; their prefix locates site-packages.
        result[name] = Path(value).absolute()
    return result
