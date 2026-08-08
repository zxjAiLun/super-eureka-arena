"""Deploy-artifact contract tests.

Regression checks on the deployed server artifacts in this standalone Arena
repository:

- the API unit must start the application through its factory
  (chessarena.main:create_app --factory).  The API module exposes
  create_app(), not a module-level app, so the obsolete
  chessarena.main:app target crashes the service at bootstrap
  (uvicorn: Attribute "app" not found).
- the release wrapper (deploy/arena-deploy.sh) must normalize its working
  directory and run pip/Alembic from inside the release directory.
- the nginx snippet keeps /admin/ and /api/v1/ behind Basic Auth while the
  public replay subtree stays anonymous.

GitHub SSH deploy workflows are NOT part of this contract: the engine's
immutable-artifact publishing lives in the Engine repository, and the
deprecated GitHub SSH deploy path is not restored here.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
API_UNIT = REPO_ROOT / "deploy" / "chessarena-api.service"
DEPLOY_WRAPPER = REPO_ROOT / "deploy" / "arena-deploy.sh"
NGINX_SNIPPET = REPO_ROOT / "deploy" / "nginx-chessarena.conf"


def test_api_unit_starts_through_the_application_factory():
    assert API_UNIT.is_file(), f"missing {API_UNIT}"
    content = API_UNIT.read_text(encoding="utf-8")
    exec_line = next(
        line for line in content.splitlines() if line.startswith("ExecStart=")
    )

    assert "chessarena.main:create_app" in exec_line
    assert "--factory" in exec_line
    assert "chessarena.main:app" not in exec_line


def test_deploy_wrapper_normalizes_working_directories():
    """pip/python/alembic inherit sys.path[0]='' resolved against the caller
    cwd.  The wrapper is invoked by the deploy user through sudo, whose SSH
    session starts in /home/deploy (750, not accessible to chessarena), so a
    bare 'sudo -u chessarena pip' crashes with PermissionError during pip's
    initial distribution scan.  The wrapper must therefore normalize its cwd
    to /opt/chessarena up front and run pip/Alembic from inside the release
    directory (alembic.ini's script_location is relative to the cwd)."""
    assert DEPLOY_WRAPPER.is_file(), f"missing {DEPLOY_WRAPPER}"
    content = DEPLOY_WRAPPER.read_text(encoding="utf-8")
    lines = content.splitlines()

    def line_index(needle: str) -> int:
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith("#"):
                continue  # ignore comments that mention commands
            if needle in ln:
                return i
        return -1

    first_sudo = line_index("sudo -u chessarena")
    cd_root = line_index("cd /opt/chessarena")
    assert cd_root != -1, "wrapper must cd to /opt/chessarena"
    assert cd_root < first_sudo, (
        "wrapper must enter /opt/chessarena before any sudo -u chessarena"
    )

    cd_dest = line_index('cd "$dest"')
    assert cd_dest != -1, "release-install must enter the release directory"
    pip_index = line_index('pip" install -e .')
    assert pip_index != -1, "pip must run editable-install from the release dir"
    assert cd_dest < pip_index, "cd \"$dest\" must precede pip install -e ."

    alembic_index = line_index(
        '"$VENV/bin/alembic" -c alembic.ini upgrade head'
    )
    assert alembic_index != -1, "alembic must use the release-local alembic.ini"
    assert cd_dest < alembic_index, "alembic must run after cd \"$dest\""

    # Neither pip nor Alembic may bypass the normalized cwd via an absolute
    # target that would let an arbitrary caller cwd leak in.
    assert '"$VENV/bin/pip" install -e "$dest"' not in content
    assert '"$dest/alembic.ini"' not in content


def _nginx_locations(content: str) -> dict[str, str]:
    """Parse each ``location <path> { ... }`` block from the nginx snippet."""
    locations: dict[str, str] = {}
    import re as _re

    for m in _re.finditer(r"location\s+(\S+)\s*{(.*?)}", content, _re.DOTALL):
        locations[m.group(1)] = m.group(2)
    return locations


def test_nginx_auth_split_keeps_admin_and_api_private():
    """P4.1 auth split: /admin/ and /api/v1/ stay behind Basic Auth; the
    public replay subtree (/static/, root, /matches/, /games/, /public-api/)
    must be anonymous.  proxy_pass must stay trailing-slash-free so the app's
    base path is preserved."""
    assert NGINX_SNIPPET.is_file(), f"missing {NGINX_SNIPPET}"
    content = NGINX_SNIPPET.read_text(encoding="utf-8")
    locations = _nginx_locations(content)

    assert "/chessarena/admin/" in locations
    assert "/chessarena/api/v1/" in locations
    assert "/chessarena/static/" in locations
    assert "/chessarena/" in locations

    admin = locations["/chessarena/admin/"]
    api = locations["/chessarena/api/v1/"]
    public_static = locations["/chessarena/static/"]
    public_root = locations["/chessarena/"]

    assert "auth_basic" in admin, "admin location must require Basic Auth"
    assert "auth_basic_user_file" in admin
    assert "auth_basic" in api, "management API location must require Basic Auth"
    assert "auth_basic" not in public_static, "static must stay public"
    assert "auth_basic" not in public_root, "public pages must stay anonymous"

    for block in locations.values():
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("proxy_pass"):
                assert "://" in stripped and not stripped.rstrip(";").endswith("/"), (
                    "proxy_pass must have no trailing slash (base path passthrough)"
                )
