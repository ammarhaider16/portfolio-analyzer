import json
import os
import subprocess
import sys


def test_offline_cli_without_credentials(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PORTFOLIO_", "LANGCHAIN_", "LANGSMITH_")) and key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LITELLM_API_KEY"}}
    command = [sys.executable, "-m", "portfolio_analyzer", "--data-dir", str(tmp_path), "analyze", "--demo", "--offline"]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, env=env)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["schema_version"] == "2.0"
    assert result["status"] == "success"
    assert result["metrics"]["known_value"] == "19450"
    assert result["suggestions"] == []
    assert any("DEMONSTRATION PORTFOLIO" in warning for warning in result["warnings"])


def test_cli_schema_is_valid_json():
    result = subprocess.run([sys.executable, "-m", "portfolio_analyzer", "schema", "request"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "snapshot" in json.loads(result.stdout)["properties"]
