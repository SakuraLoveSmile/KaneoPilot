from pathlib import Path
import sys

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO / "core") not in sys.path:
    sys.path.insert(0, str(_REPO / "core"))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import tests.conftest as _tc

# Re-export fixtures from tests.conftest so they are discovered when running in core/tests
factory = _tc.factory
cli = _tc.cli
tmp_env = _tc.tmp_env
fake_kaneo = _tc.fake_kaneo
legacy_state = _tc.legacy_state
