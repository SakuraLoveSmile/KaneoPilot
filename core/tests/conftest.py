from pathlib import Path
import os
import sys
import tempfile

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO / "core") not in sys.path:
    sys.path.insert(0, str(_REPO / "core"))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# §5.33：测试不读操作者本机的 Magpie 接管文件（~/.config、Application Support 里那份）。
# 接管相关用例自己 mock KANPILOT_MAGPIE_AGENT，其余用例按「未接管」跑。
_isolation = Path(tempfile.gettempdir()) / "kanpilot-tests-no-such-agent.json"
try:
    _isolation.unlink()
except OSError:
    pass
os.environ.setdefault("KANPILOT_MAGPIE_AGENT", str(_isolation))

import tests.conftest as _tc

# Re-export fixtures from tests.conftest so they are discovered when running in core/tests
factory = _tc.factory
cli = _tc.cli
tmp_env = _tc.tmp_env
seed_project_configuration = _tc.seed_project_configuration
fake_kaneo = _tc.fake_kaneo
legacy_state = _tc.legacy_state
