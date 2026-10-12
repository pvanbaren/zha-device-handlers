"""Control4 Zigbee quirks.

Installed only through ZHA's ``custom_quirks_path`` (see README). This file is
the package's loader. The modules import each other by bare name
(``import c4_helpers``), and ZHA's custom loader executes ``control4.X`` with
``exec_module`` regardless of ``sys.modules``, so without this every file ran
twice: two copies of every patch, quirk class and device. Instead, import each
sibling once by bare name, alias it as ``control4.X``, then empty ``__path__``
so ``zhaquirks.setup()``'s walk finds nothing left to execute.

Imported as part of the zhaquirks distribution it does nothing, so a stock
install never loads ``c4_hooks`` or its zigpy patches.
"""

import importlib
import logging
import os
import pkgutil
import sys

_LOGGER = logging.getLogger(__name__)

if not __name__.startswith("zhaquirks."):
    _DIR = os.path.dirname(os.path.abspath(__file__))
    if _DIR not in sys.path:
        sys.path.insert(0, _DIR)
    _names = [m.name for m in pkgutil.iter_modules([_DIR])]
    # Start a fresh generation: a ZHA reload purges these quirks from the
    # registry, and only re-executing the modules registers them again.
    for _n in _names:
        sys.modules.pop(_n, None)
        sys.modules.pop(f"{__name__}.{_n}", None)
    for _n in _names:
        try:
            # A sys.modules hit if a sibling already pulled it in.
            _mod = importlib.import_module(_n)
        except Exception:  # never let one bad module stop the rest
            _LOGGER.exception("C4: failed to import %s", _n)
            continue
        sys.modules[f"{__name__}.{_n}"] = _mod
        globals()[_n] = _mod
    _LOGGER.debug("C4: loaded %d modules once each", len(_names))

__path__ = []  # nothing left for walk_packages to find
