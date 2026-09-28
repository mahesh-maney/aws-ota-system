#!/usr/bin/env python3
"""
make_test_artifact.py
Builds a test OTA artefact: a .tar containing manifest.json plus a padding
payload, sized to an exact byte count.

Usage:
  ./make_test_artifact.py                                  # 10 MB ZigbeeFirmware-4.2.2.tar
  ./make_test_artifact.py --version 4.2.3 --size 25MB      # multipart-sized artefact
  ./make_test_artifact.py --device-type Network_controller_firmware

The tar lands in test-artifacts/ and the SHA256 it prints is the value to pass
as `checksum` to POST /ota/packages/upload-artefact. Sizes > 10 MB make the
upload-url Lambda return a MULTIPART session instead of a single PUT URL.
"""
import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
import time

# deviceType → (packageName, extension, operationType) — mirrors DEVICE_TYPE_MAP
# in infrastructure/06_lambdas/digilux_ota_upload_url/lambda_function.py
DEVICE_TYPES = {
    "Network_controller_firmware":             ("HomeAssistantUtility",  ".jar", 1),
    "Network_controller_zigbee_firmware":      ("ZigbeeFirmware",        ".tar", 2),
    "Network_controller_Z2M_Firmware":         ("Z2MFirmware",           ".bin", 3),
    "Network_controller_Miscellaneous":        ("NetControllerMisc",     ".py",  4),
    "Network_controller_zigbee_stack_firmware": ("ZigbeeStackFirmware",  ".bin", 5),
}

PAYLOAD_NAME = "firmware.bin"


def parse_size(text: str) -> int:
    """'10MB' → 10485760, '1048576' → 1048576."""
    t = text.strip().upper().replace("IB", "B")
    for suffix, mult in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024), ("B", 1)):
        if t.endswith(suffix):
            return int(float(t[: -len(suffix)]) * mult)
    return int(t)


def build_tar(path: str, manifest: dict, payload_size: int) -> None:
    manifest_bytes = json.dumps(manifest, indent=2).encode()
    mtime = int(time.time())

    with tarfile.open(path, "w", format=tarfile.GNU_FORMAT) as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mtime, info.mode = len(manifest_bytes), mtime, 0o644
        tar.addfile(info, io.BytesIO(manifest_bytes))

        info = tarfile.TarInfo(PAYLOAD_NAME)
        info.size, info.mtime, info.mode = payload_size, mtime, 0o644
        tar.addfile(info, _zeros(payload_size))


class _zeros(io.RawIOBase):
    """Streams `total` zero bytes so a large payload never sits in memory."""

    def __init__(self, total: int):
        self.remaining = total

    def readable(self) -> bool:
        return True

    def readinto(self, buf) -> int:
        n = min(len(buf), self.remaining)
        buf[:n] = b"\0" * n
        self.remaining -= n
        return n


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    ap = argparse.ArgumentParser()
    ap.add_argument("--device-type", default="Network_controller_zigbee_firmware",
                    choices=sorted(DEVICE_TYPES))
    ap.add_argument("--version", default="4.2.2")
    ap.add_argument("--size", default="10MB", help="exact tar size, e.g. 10MB / 25MB / 1048576")
    ap.add_argument("--release-type", default="PROD", choices=["PROD", "BETA", "CUSTOM"])
    ap.add_argument("--out-dir", default=os.path.join(repo_root, "test-artifacts"))
    args = ap.parse_args()

    package_name, ext, operation_type = DEVICE_TYPES[args.device_type]
    target = parse_size(args.size)
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, f"{package_name}-{args.version}{ext}")

    manifest = {
        "packageName":   package_name,
        "version":       args.version,
        "deviceType":    args.device_type,
        "operationType": operation_type,
        "releaseType":   args.release_type,
        "fileName":      os.path.basename(out_path),
        "createdAt":     int(time.time() * 1000),
        "files":         [{"path": PAYLOAD_NAME, "role": "firmware"}],
    }

    # Tar headers pad to 512-byte records and the archive to a 10240-byte block,
    # so solve for the payload size that lands on `target` exactly.
    payload_size = target
    for _ in range(8):
        build_tar(out_path, manifest, max(payload_size, 0))
        actual = os.path.getsize(out_path)
        if actual == target:
            break
        payload_size -= actual - target
    else:
        print(f"Could not hit {target} bytes exactly (got {actual})", file=sys.stderr)
        return 1

    if payload_size <= 0:
        print(f"--size {args.size} is too small for the tar headers", file=sys.stderr)
        return 1

    print(f"{out_path}\n  size    {actual} bytes ({actual / 1024 / 1024:.2f} MB)"
          f"\n  payload {payload_size} bytes in {PAYLOAD_NAME}"
          f"\n  sha256  {sha256_of(out_path)}"
          f"\n  upload  {'MULTIPART' if actual > 10 * 1024 * 1024 else 'SINGLE'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
