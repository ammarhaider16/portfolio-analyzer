"""Build a native console bundle and ZIP. Run with uv run --group packaging."""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    system = platform.system()
    architecture = platform.machine().lower().replace("amd64", "x86_64")
    if system not in {"Windows", "Darwin"}:
        raise SystemExit("Build on Windows or macOS; cross-compilation is not supported.")
    name = "Betafolio" if system == "Windows" else "betafolio"
    label = f"betafolio-{'windows' if system == 'Windows' else 'macos'}-{architecture}"
    work = ROOT / "build" / label
    dist = ROOT / "dist" / label
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
        "--name", name, "--paths", str(ROOT / "src"),
        "--additional-hooks-dir", str(ROOT / "packaging" / "hooks"),
        "--distpath", str(dist), "--workpath", str(work), "--specpath", str(work),
        "--exclude-module", "pytest", "--exclude-module", "tkinter",
        str(ROOT / "src" / "betafolio" / "packaged.py"),
    ], check=True, cwd=ROOT)
    bundle = dist / name
    shutil.copy2(ROOT / "LICENSE", bundle / "LICENSE.txt")
    (bundle / "START-HERE.txt").write_text(
        "Betafolio private preview\n\n"
        f"Start {'Betafolio.exe' if system == 'Windows' else 'Start Betafolio.command'} and open the local URL printed in the terminal.\n"
        "Keep the terminal open. Stop with Ctrl+C. Scheduled research requires this process and an awake computer.\n"
        "Extract the entire ZIP before starting. Keep all supporting files together.\n"
        "Settings and credentials are stored separately from this folder.\n"
        "This preview has no publisher signature. Obtain it only from the trusted project distributor.\n"
        "macOS may block downloaded unsigned software; use the documented OS approval flow only if you trust its origin.\n",
        encoding="utf-8",
    )
    if system == "Darwin":
        launcher = bundle / "Start Betafolio.command"
        launcher.write_text('#!/bin/sh\ncd -- "$(dirname -- "$0")" || exit 1\nexec ./betafolio "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    archive = ROOT / "dist" / f"{label}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(bundle.rglob("*")):
            if path.is_symlink():
                entry = zipfile.ZipInfo(path.relative_to(dist).as_posix())
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                zipped.writestr(entry, os.readlink(path))
            elif path.is_file():
                zipped.write(path, path.relative_to(dist))
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"Built {archive} ({archive.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
