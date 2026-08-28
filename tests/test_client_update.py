import subprocess
import time
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "client" / "update-check.sh"


def run_check(client_dir: Path, version: str, remote_version: str) -> subprocess.CompletedProcess[str]:
    command = f'''\
source "{SCRIPT}"
curl() {{ printf '%s\\n' "$PKGRELAY_FAKE_REMOTE_VERSION"; }}
PKGRELAY_CLIENT_DIR="{client_dir}"
PKGRELAY_CLIENT_VERSION="{version}"
PKGRELAY_URL="http://gateway.test"
PKGRELAY_FAKE_REMOTE_VERSION="{remote_version}"
_pkgrelay_check_client_update
'''
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True)


def test_outdated_client_is_notified_after_check_interval(tmp_path: Path):
    (tmp_path / "update-check-at").write_text("0\n")

    result = run_check(tmp_path, "old-version", "new-version")

    assert result.returncode == 42
    assert "package command blocked" in result.stderr


def test_recent_previous_check_does_not_allow_an_outdated_client(tmp_path: Path):
    (tmp_path / "update-check-at").write_text(f"{int(time.time())}\n")

    result = run_check(tmp_path, "old-version", "new-version")

    assert result.returncode == 42
    assert "package command blocked" in result.stderr


def test_current_client_is_allowed(tmp_path: Path):
    result = run_check(tmp_path, "current-version", "current-version")

    assert result.returncode == 0
    assert result.stderr == ""
