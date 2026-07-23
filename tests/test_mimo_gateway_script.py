import json
import os
import pathlib
import subprocess
import tempfile
from unittest.mock import patch

from src import gateway_encryption
from tests.integration import security_gateway_checks


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_local_launcher_defaults_to_loopback_and_auto_exposure():
    script = (ROOT / "scripts/mimo_gateway.sh").read_text(encoding="utf-8")

    assert 'HOST="${GATEWAY_HOST:-127.0.0.1}"' in script
    assert 'GATEWAY_PUBLIC_EXPOSURE="${GATEWAY_PUBLIC_EXPOSURE:-auto}"' in script


def test_status_uses_decrypted_client_key_from_encrypted_config():
    with tempfile.TemporaryDirectory() as td:
        with patch.dict(os.environ, {"GATEWAY_RUNTIME_DIR": td}, clear=False):
            with patch.object(gateway_encryption, "_encryption_key", None), patch.object(
                gateway_encryption, "_fernet", None
            ):
                config = gateway_encryption.encrypt_config(
                    {
                        "gateway": {
                            "client_snippet_api_key": "test-downstream-key",
                            "public_base_url": "http://127.0.0.1:65534",
                        }
                    }
                )
        config_path = pathlib.Path(td) / "gateway.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "GATEWAY_CONFIG_PATH": str(config_path),
                "GATEWAY_PORT": "65534",
                "GATEWAY_RUNTIME_DIR": td,
            }
        )
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/mimo_gateway.sh"), "status"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    assert "API key: test***" in result.stdout
    assert "API key: encr***" not in result.stdout


def test_generated_config_encrypts_credentials_and_launchd_plist_omits_them():
    with tempfile.TemporaryDirectory() as td:
        config_path = pathlib.Path(td) / "gateway.json"
        env = os.environ.copy()
        env.update(
            {
                "GATEWAY_CONFIG_PATH": str(config_path),
                "GATEWAY_RUNTIME_DIR": td,
                "DOWNSTREAM_API_KEY": "downstream-runtime-secret",
                "UPSTREAM_API_KEY": "upstream-runtime-secret",
                "UPSTREAM_BASE_URL": "https://upstream.invalid",
                "UPSTREAM_MODEL": "test-model",
            }
        )
        subprocess.run(
            ["bash", str(ROOT / "scripts/mimo_gateway.sh"), "config"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

        raw = config_path.read_text(encoding="utf-8")
        assert "downstream-runtime-secret" not in raw
        assert "upstream-runtime-secret" not in raw
        assert (config_path.stat().st_mode & 0o777) == 0o600

        with patch.dict(os.environ, {"GATEWAY_RUNTIME_DIR": td}, clear=False), patch.object(
            gateway_encryption, "_encryption_key", None
        ), patch.object(gateway_encryption, "_fernet", None):
            decrypted = gateway_encryption.decrypt_config(json.loads(raw), in_place=False)
        assert decrypted["gateway"]["client_snippet_api_key"] == "downstream-runtime-secret"
        assert decrypted["upstream"]["api_key"] == "upstream-runtime-secret"

    script = (ROOT / "scripts/mimo_gateway.sh").read_text(encoding="utf-8")
    assert "<key>UPSTREAM_API_KEY</key>" not in script
    assert "<key>DOWNSTREAM_API_KEY</key>" not in script
    assert "<key>GATEWAY_RUNTIME_DIR</key>" in script


def test_verify_uses_trusted_local_overrides_without_changing_start_defaults():
    script = (ROOT / "scripts/mimo_gateway.sh").read_text(encoding="utf-8")
    before_verify, verify_body = script.split("verify_all() {", 1)
    launchd_body = before_verify.split("write_launchd_plist() {", 1)[1].split("start_launchd() {", 1)[0]

    assert 'GATEWAY_EXECUTE_USER_SIDE_TOOLS="${GATEWAY_VERIFY_EXECUTE_USER_SIDE_TOOLS:-1}"' in verify_body
    assert 'GATEWAY_ALLOW_PRIVATE_NETWORK_TOOLS="${GATEWAY_VERIFY_ALLOW_PRIVATE_NETWORK_TOOLS:-1}"' in verify_body
    assert "GATEWAY_EXECUTE_USER_SIDE_TOOLS GATEWAY_ALLOW_PRIVATE_NETWORK_TOOLS" in verify_body
    assert "\n  env \\\n" not in verify_body
    assert "python3 -m unittest discover" in verify_body
    assert "local unit_status=0" in verify_body
    assert 'rm -rf -- "$test_dir"' in verify_body
    assert "cli_args[@]" not in verify_body
    assert 'project_scope_cli_smoke.py" --require-claude --require-codex' in verify_body
    stage_five = verify_body.split('echo "== 5/5 Claude/Codex project-scope smoke =="', 1)[1]
    assert "unset GATEWAY_EXECUTE_USER_SIDE_TOOLS GATEWAY_ALLOW_PRIVATE_NETWORK_TOOLS" in stage_five
    assert "GATEWAY_EXECUTE_USER_SIDE_TOOLS" not in before_verify
    assert "GATEWAY_ALLOW_PRIVATE_NETWORK_TOOLS" not in before_verify
    assert "GATEWAY_VERIFY_" not in launchd_body


def test_security_verification_resolves_admin_auth_from_environment_without_argv():
    with patch.dict(
        os.environ,
        {
            "GATEWAY_VERIFY_ADMIN_USERNAME": "verify-admin",
            "GATEWAY_VERIFY_ADMIN_PASSWORD": "verify-password",
        },
        clear=True,
    ):
        assert security_gateway_checks.resolve_admin_auth() == "verify-admin:verify-password"

    with patch.dict(os.environ, {"GATEWAY_VERIFY_ADMIN_AUTH": "combined-admin:combined-password"}, clear=True):
        assert security_gateway_checks.resolve_admin_auth() == "combined-admin:combined-password"

    assert security_gateway_checks.resolve_admin_auth("cli-admin:cli-password") == "cli-admin:cli-password"


def test_verify_admin_secret_is_not_added_to_security_check_argv_or_launchd_plist():
    script = (ROOT / "scripts/mimo_gateway.sh").read_text(encoding="utf-8")
    verify_body = script.split("verify_all() {", 1)[1]
    launchd_body = script.split("write_launchd_plist() {", 1)[1].split("start_launchd() {", 1)[0]

    security_call = next(line for line in verify_body.splitlines() if "security_gateway_checks.py" in line)
    assert "--admin" not in security_call
    assert "GATEWAY_VERIFY_ADMIN_PASSWORD" not in launchd_body
    assert "GATEWAY_VERIFY_ADMIN_AUTH" not in launchd_body
