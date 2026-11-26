"""Shared native workspace, browser and process discovery."""
import hashlib
import os
from pathlib import Path
import shutil

APP_ROOT = Path(__file__).resolve().parents[1]
ENGINE = APP_ROOT / "engine"


def location(name, fallback):
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else fallback


ROOT = location("VRS_WORKSPACE", APP_ROOT)
CACHE = location("VRS_CACHE_DIR", ROOT / "render-cache")
MODELS = location("VRS_MODEL_DIR", CACHE / "whisper")
WORKSPACE_ID = hashlib.sha256(os.fsencode(os.path.normcase(str(ROOT)))).hexdigest()


def browser_path(configured=None):
    configured = os.environ.get("HYPERFRAMES_BROWSER_PATH") or configured
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_file() and os.access(path, os.X_OK) else None
    candidates = []
    if os.name == "nt":
        for key in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(key):
                base = Path(os.environ[key])
                candidates.extend(base / name for name in (
                    "Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"))
    else:
        candidates.append(Path("/opt/google/chrome/chrome"))
    for cmd in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        if path := shutil.which(cmd):
            candidates.append(Path(path))
    return next((p.resolve() for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)


def process_alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return not stat.is_file() or stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False
    except PermissionError:
        return True
