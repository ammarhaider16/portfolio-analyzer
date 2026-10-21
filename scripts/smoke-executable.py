"""Exercise a native executable without network calls or user configuration."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--archive", type=Path, help="Validate and extract the ZIP before exercising its executable")
    args = parser.parse_args()
    executable = args.executable.resolve()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PORTFOLIO_", "LANGCHAIN_", "LANGSMITH_"))
           and key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LITELLM_API_KEY", "PYTHONPATH", "PYTHONHOME"}}
    with tempfile.TemporaryDirectory(prefix="Betafolio packaged smoke ") as directory:
        if args.archive:
            archive = args.archive.resolve()
            with zipfile.ZipFile(archive) as zipped:
                names = zipped.namelist()
                forbidden = {".tools", ".git", "tests", "deployment-secrets", "settings.json", "last_run.html"}
                assert not any(forbidden.intersection(Path(name).parts) or name.endswith((".sqlite3", ".env"))
                               for name in names), "Unexpected development or private files in ZIP"
            extracted = Path(directory) / "extracted"
            extracted.mkdir()
            if os.name == "nt":
                with zipfile.ZipFile(archive) as zipped:
                    zipped.extractall(extracted)
                executable = extracted / "Betafolio" / "Betafolio.exe"
            else:
                subprocess.run(["ditto", "-x", "-k", str(archive), str(extracted)], check=True)
                executable = extracted / "betafolio" / "betafolio"
        result = subprocess.run([str(executable), "--data-dir", directory, "analyze", "--demo", "--offline"],
                                capture_output=True, text=True, timeout=90, env=env)
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["status"] == "success", report
        assert report["metrics"]["known_value"] == "19450", report["metrics"]
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        with tempfile.TemporaryFile(mode="w+") as log:
            process = subprocess.Popen([str(executable), "--data-dir", directory, "serve", "--port", str(port)],
                                       stdout=log, stderr=log, env=env, **flags)
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        log.seek(0)
                        raise AssertionError(log.read())
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                            assert b"Betafolio" in response.read()
                        with urlopen(f"http://127.0.0.1:{port}/static/app.css", timeout=2) as response:
                            assert response.status == 200
                        with urlopen(f"http://127.0.0.1:{port}/api/status", timeout=10) as response:
                            status = json.load(response)
                            assert "settings" in status and "secret_storage" in status
                        break
                    except OSError:
                        time.sleep(0.25)
                else:
                    raise AssertionError("Packaged server did not start")
            finally:
                if process.poll() is None:
                    process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        raise AssertionError("Packaged server did not shut down after interrupt") from None
        print("Packaged offline analysis, dashboard assets, and shutdown passed.")


if __name__ == "__main__":
    main()
