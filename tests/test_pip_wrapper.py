import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "client" / "pip-wrapper.sh"


def source_wrapper(url: str, client_id: str = "a" * 32) -> dict[str, str]:
    command = f'''\
PKGRELAY_URL="{url}"
PKGRELAY_CLIENT_ID="{client_id}"
PKGRELAY_CLIENT_DIR="/nonexistent"
unset PIP_INDEX_URL PIP_NO_CACHE_DIR PIP_TRUSTED_HOST
source "{SCRIPT}"
printf 'index=%s\\nno_cache=%s\\ntrusted=%s\\n' \\
  "${{PIP_INDEX_URL:-}}" "${{PIP_NO_CACHE_DIR:-}}" "${{PIP_TRUSTED_HOST:-}}"
'''
    result = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=True
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def test_python_module_pip_is_not_configured_by_the_shell_wrapper():
    values = source_wrapper("http://gateway.test:45612")

    assert values == {"index": "", "no_cache": "", "trusted": ""}


def test_https_gateway_also_leaves_module_pip_unconfigured():
    values = source_wrapper("https://gateway.test")

    assert values == {"index": "", "no_cache": "", "trusted": ""}
