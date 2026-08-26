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
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=True)


def test_outdated_client_is_notified_after_check_interval(tmp_path: Path):
    (tmp_path / "update-check-at").write_text("0\n")

    result = run_check(tmp_path, "old-version", "new-version")

    assert "PkgRelay client update available" in result.stderr
    assert (tmp_path / "update-notified-version").read_text() == "new-version\n"


def test_update_check_is_throttled_for_only_one_minute(tmp_path: Path):
    (tmp_path / "update-check-at").write_text(f"{int(time.time())}\n")

    result = run_check(tmp_path, "old-version", "new-version")

    assert result.stderr == ""
    assert not (tmp_path / "update-notified-version").exists()
