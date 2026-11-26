"""Dedicated production credential storage outside the portable workspace."""

import json
import os
from pathlib import Path
import stat
import subprocess
from tempfile import NamedTemporaryFile

from studio_paths import APP_ROOT, ROOT, WORKSPACE_ID


def location():
    directory = Path.home() / ".viral-reel-studio-credentials"
    resolved = directory.resolve()
    if any(resolved.is_relative_to(root.resolve()) for root in (APP_ROOT, ROOT)):
        raise ValueError("Credential storage must be outside the application and workspace.")
    return directory / (WORKSPACE_ID + ".credential")


def protect(data, *, encrypt):
    if os.name != "nt":
        return data
    # Windows DPAPI binds the encrypted file to the current Windows user.
    operation = "Protect" if encrypt else "Unprotect"
    script = (
        "Add-Type -AssemblyName System.Security; "
        "$data=[Convert]::FromBase64String([Console]::In.ReadToEnd()); "
        "[Convert]::ToBase64String([Security.Cryptography.ProtectedData]::"
        + operation + "($data,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser))"
    )
    import base64
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            input=base64.b64encode(data), capture_output=True, check=True, timeout=15,
        )
        return base64.b64decode(result.stdout.strip(), validate=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValueError("Windows could not access the protected credential.") from None


def private(path, *, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or
            (os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
        raise ValueError("Credential storage has unsafe ownership or permissions.")


def load():
    try:
        path = location()
        private(path.parent, directory=True)
        private(path)
        record = json.loads(protect(path.read_bytes(), encrypt=False))
        if not isinstance(record, dict) or not isinstance(record.get("token"), str):
            raise ValueError("The local GitHub credential has an invalid format.")
        return record
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("The local GitHub credential could not be read.") from None


def save(record):
    path = location()
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        private(path.parent, directory=True)
        try:
            private(path)
        except FileNotFoundError:
            pass
        payload = protect(json.dumps(record).encode(), encrypt=True)
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise ValueError("The local GitHub credential could not be saved.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
