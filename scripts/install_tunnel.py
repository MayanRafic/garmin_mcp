"""Install an official tunnel-client release after checking its published SHA256."""

import hashlib
import io
import os
from pathlib import Path
import platform
import urllib.request
import zipfile


def install(destination: Path, version: str = "0.0.18") -> None:
    architectures = {"x86_64": "amd64", "aarch64": "arm64"}
    architecture = architectures[platform.machine()]
    name = f"tunnel-client-v{version}-linux-{architecture}.zip"
    base = f"https://github.com/openai/tunnel-client/releases/download/v{version}"
    with urllib.request.urlopen(f"{base}/SHA256SUMS.txt", timeout=60) as response:
        lines = response.read().decode().splitlines()
    checksums = {parts[-1].lstrip("*"): parts[0] for line in lines if (parts := line.split())}
    with urllib.request.urlopen(f"{base}/{name}", timeout=120) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != checksums.get(name):
        raise ValueError("Tunnel client checksum mismatch")
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for binary in ("tunnel-client", "cloudflared"):
            (destination / binary).write_bytes(bundle.read(binary))
            (destination / binary).chmod(0o755)


if __name__ == "__main__":
    install(Path(os.getenv("TUNNEL_INSTALL_DIR", "/usr/local/bin")))
