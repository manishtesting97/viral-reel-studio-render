"""Phase 4B wire contract and bounded ZIP transfer, independent of the transport."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import select
import stat
import time
from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED

VERSION = 1
CHUNK = 128 * 1024
MANIFEST_LIMIT = 512 * 1024
MAX_FILES = 2048
HEX = re.compile(r"[a-f0-9]{64}")
PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def hash_file(path):
    with path.open("rb") as src:
        digest = hashlib.sha256()
        for chunk in iter(lambda: src.read(CHUNK), b""):
            digest.update(chunk)
        return digest.hexdigest()


def job_id(manifest):
    return hashlib.sha256(encode(manifest)).hexdigest()


def valid_id(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError("Invalid job ID or SHA-256.")
    return value


def safe_path(name):
    if not isinstance(name, str) or len(name) > 240 or not all(PATH.fullmatch(p) for p in name.split("/")):
        raise ValueError("Invalid package path.")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError("Invalid package path.")
    return path


def entries(files, limit):
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise ValueError("Invalid file inventory.")
    seen = set()
    total = 0
    for name, item in files.items():
        path = safe_path(name)
        folded = name.lower()
        if folded in seen or any(str(parent).lower() in seen for parent in path.parents):
            raise ValueError("Conflicting package names.")
        seen.add(folded)
        if not isinstance(item, dict) or set(item) != {"bytes", "sha256"}:
            raise ValueError("Invalid file inventory entry.")
        valid_id(item["sha256"])
        if type(item["bytes"]) is not int or item["bytes"] < 0:
            raise ValueError("Invalid file size.")
        total += item["bytes"]
    for name in seen:
        if any(str(parent) in seen for parent in PurePosixPath(name).parents):
            raise ValueError("Conflicting package names.")
    if total > limit:
        raise ValueError("Package exceeds the byte limit.")


def validate(manifest, limit):
    if not isinstance(manifest, dict) or set(manifest) != {
        "version", "operation", "preset", "revision", "language", "files"
    } or type(manifest["version"]) is not int or manifest["version"] != VERSION:
        raise ValueError("Unsupported job manifest.")
    valid_id(manifest["revision"])
    files = manifest["files"]
    entries(files, limit)
    op, preset, language = (manifest[k] for k in ("operation", "preset", "language"))
    if op == "transcribe":
        if (preset is not None or language not in ("en", "hi", "hinglish", "auto")
                or "voice.wav" not in files
                or any(name not in {"voice.wav", "recovery.json"} for name in files)):
            raise ValueError("Invalid transcription inputs.")
    elif op == "render":
        if preset not in ("draft", "standard", "premium") or language is not None or "index.html" not in files:
            raise ValueError("Invalid render inputs.")
        if any(name not in {"index.html", "voice.wav", "captions.ass"} and not name.startswith("assets/")
               for name in files):
            raise ValueError("Unexpected render file.")
    elif op == "prepare":
        if preset is not None or language is not None:
            raise ValueError("Invalid scene preparation inputs.")
        if not any(re.fullmatch(r"projects/[A-Za-z0-9_.-]+/plan\.json", name) for name in files):
            raise ValueError("Invalid scene preparation inputs.")
        if any(not (name.startswith("projects/") or name.startswith("brand-profiles/")
                    or name.startswith("style-library/") or name.startswith("assets/"))
               for name in files):
            raise ValueError("Unexpected scene preparation file.")
    else:
        raise ValueError("Unsupported operation.")


def copy_bounded(src, dest, limit):
    count = 0
    for chunk in iter(lambda: src.read(CHUNK), b""):
        count += len(chunk)
        if count > limit:
            raise ValueError("Transfer exceeds the byte limit.")
        dest.write(chunk)
    return count


DEFAULT_LIMITS = dict(max_transfer=512*1024**2, max_storage=3*1024**3, min_cpu=2,
                      min_ram=8*1024**3, min_disk=20*1024**3, wall_seconds=1800, retention_seconds=86400)


def copy_pipe(src, dest, limit, timeout, progress=None):
    deadline = time.monotonic() + timeout
    count = 0
    while time.monotonic() < deadline:
        if os.name == "nt":
            # Winsock select cannot wait on a subprocess's anonymous pipe.
            import ctypes
            import msvcrt
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.PeekNamedPipe.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
            pending = wintypes.DWORD()
            if not kernel.PeekNamedPipe(msvcrt.get_osfhandle(src.fileno()), None, 0, None, ctypes.byref(pending), None):
                if ctypes.get_last_error() in {109, 232}:
                    return count
                raise ctypes.WinError(ctypes.get_last_error())
            if not pending.value:
                time.sleep(min(.025,max(0,deadline-time.monotonic())))
                continue
            chunk = os.read(src.fileno(), min(CHUNK,pending.value))
        else:
            if not select.select([src], [], [], max(0, deadline-time.monotonic()))[0]:
                break
            chunk = os.read(src.fileno(), CHUNK)
        if not chunk:
            return count
        count += len(chunk)
        if count > limit:
            raise ValueError("Transfer exceeds the byte limit.")
        dest.write(chunk)
        if progress:
            progress(count,limit)
        if time.monotonic() >= deadline:
            break
    raise ValueError("Transfer deadline exceeded.")


def write_package(output, root, operation, revision, limit, preset=None, language=None):
    if operation == "prepare":
        names = []
        for top in ("projects", "brand-profiles", "style-library", "assets"):
            base = root / top
            if base.is_symlink():
                raise ValueError("Inputs must not be symlinked.")
            if base.is_dir():
                for path in base.rglob("*"):
                    if path.is_symlink() or not (path.is_file() or path.is_dir()):
                        raise ValueError("Preparation inputs must be regular files or directories.")
                    if path.is_file():
                        names.append(path.relative_to(root).as_posix())
    else:
        if operation == "transcribe":
            names = ["voice.wav", *(["recovery.json"] if (root / "recovery.json").is_file() else [])]
        else:
            names = ["index.html", "voice.wav", "captions.ass"]
        if operation == "render":
            assets = root / "assets"
            if assets.is_symlink():
                raise ValueError("Assets must not be symlinked.")
            for path in assets.rglob("*"):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise ValueError("Assets must be regular files or directories.")
                if path.is_file():
                    names.append(path.relative_to(root).as_posix())
    files = {}
    for name in sorted(names):
        path = root / name
        if path.is_symlink():
            raise ValueError("Inputs must not be symlinked.")
        if path.is_file():
            files[name] = {"bytes": path.stat().st_size, "sha256": hash_file(path)}
    manifest = dict(version=VERSION, operation=operation, preset=preset, language=language,
                    revision=revision, files=files)
    validate(manifest, limit)
    try:
        with output.open("xb") as stream:
            try:
                with ZipFile(stream, "w", ZIP_STORED) as archive:
                    archive.writestr("manifest.json", encode(manifest))
                    for name in files:
                        archive.write(root / name, name)
                if stream.tell() > limit:
                    raise ValueError("Archive exceeds the byte limit.")
            except BaseException:
                output.unlink(missing_ok=True)
                raise
    except FileExistsError as error:
        raise ValueError("Package already exists.") from error
    return manifest


def extract_package(archive_path, destination, limit, expected_id):
    if archive_path.stat().st_size > limit:
        raise ValueError("Archive exceeds the byte limit.")
    with ZipFile(archive_path) as archive:
        inventory = archive.infolist()
        if len(inventory) > MAX_FILES + 1 or len({i.filename for i in inventory}) != len(inventory):
            raise ValueError("Invalid ZIP inventory.")
        for info in inventory:
            safe_path(info.filename)
            file_type = stat.S_IFMT(info.external_attr >> 16)
            if (info.filename != info.orig_filename or info.is_dir() or file_type not in (0, stat.S_IFREG)
                    or info.flag_bits & 1 or info.compress_type not in (ZIP_STORED,ZIP_DEFLATED)):
                raise ValueError("Only unencrypted regular ZIP entries are permitted.")
        info = archive.getinfo("manifest.json")
        if info.file_size > MANIFEST_LIMIT:
            raise ValueError("Manifest exceeds the byte limit.")
        manifest = json.loads(archive.read(info))
        validate(manifest, limit)
        if job_id(manifest) != expected_id:
            raise ValueError("Manifest job ID mismatch.")
        files = manifest["files"]
        if set(files) | {"manifest.json"} != set(archive.namelist()):
            raise ValueError("ZIP inventory does not match the manifest.")
        for name, record in files.items():
            info = archive.getinfo(name)
            if info.file_size != record["bytes"]:
                raise ValueError("Input size mismatch.")
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, path.open("xb") as dest:
                copy_bounded(src, dest, record["bytes"])
            if path.stat().st_size != record["bytes"] or hash_file(path) != record["sha256"]:
                raise ValueError("Input checksum mismatch.")
    return manifest


def reserve_output(tmp, destination):
    """Publish a verified local file atomically, with no overwrite."""
    destination.hardlink_to(tmp)
    tmp.unlink()
