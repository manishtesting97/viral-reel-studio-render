"""Private GitHub production transport. The public synthetic contract is separate."""

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zipfile import ZipFile, ZIP_STORED

import cloud
import github_credentials
from remote_protocol import copy_bounded, copy_pipe, extract_package, hash_file, valid_id

REPOSITORY = "manishtesting97/viral-reel-studio-private"
RENDERER = "manishtesting97/viral-reel-studio-render"
RENDER_BASE = f"repos/{RENDERER}"
WORKFLOW = "vrs-hybrid-production.yml"
BASE = f"repos/{REPOSITORY}"
LIMIT = 512 * 1024**2
ASSET = "input.zip"
DELIVERY = "output.zip"


TOKEN_ENV = "VRS_GITHUB_PRODUCTION_TOKEN"
SHA_ENV = "VRS_GITHUB_PRODUCTION_SHA"


def credential():
    saved = github_credentials.load()
    return saved, saved.get("token") or os.environ.get(TOKEN_ENV)


def authorization(cfg):
    saved, token = credential()
    sha = os.environ.get(SHA_ENV) or cfg.get("githubProductionSha") or saved.get("sha", "")
    if (cfg.get("privateRepo", "").casefold() != REPOSITORY
            or not re.fullmatch(r"[a-f0-9]{40}", sha) or not token):
        raise ValueError(cloud.PRODUCTION_BLOCK)
    return sha


def gh_env(runner=False, *, token=None):
    env = cloud.gh_env()
    if token is None:
        token = os.environ.get("GH_TOKEN") if runner else credential()[1]
    env.pop(TOKEN_ENV, None)
    env.pop("GH_FORCE_TTY", None)
    if not token:
        raise ValueError("Private GitHub rendering requires its scoped credential.")
    env["GH_TOKEN"] = token
    return env


def gh_request(path, *, method="GET", data=None, upload=None, runner=False,
               token=None, include=False, timeout=180):
    cmd = ["gh", "api", "--hostname", "github.com", "-H", cloud.API_VERSION, "-X", method, path]
    if include:
        cmd.append("--include")
    if data is not None:
        cmd += ["--input", "-"]
    if upload is not None:
        cmd += ["--input", str(upload), "-H", "Content-Type: application/zip"]
    return subprocess.run(cmd, input=json.dumps(data) if data is not None else "",
                          text=True, capture_output=True, env=gh_env(runner, token=token),
                          timeout=timeout)


def api(path, *, method="GET", data=None, upload=None, missing=False, runner=False):
    try:
        result = gh_request(path, method=method, data=data, upload=upload, runner=runner)
    except subprocess.TimeoutExpired as error:
        raise ValueError("Private GitHub request timed out. Reconnect to the saved job.") from error
    if result.returncode:
        if missing and "HTTP 404" in result.stderr:
            return None
        if "HTTP 401" in result.stderr:
            raise ValueError("GitHub rejected the production credential. Replace it in Settings.")
        raise ValueError("Private GitHub request failed. Check repository access, Actions quota and the saved job.")
    return json.loads(result.stdout) if result.stdout else None


def credential_status(record, token):
    status = record.get("status", "unchecked") if token else "missing"
    expires = record.get("expires")
    warning = None
    if expires:
        remaining = datetime.fromisoformat(expires) - datetime.now(timezone.utc)
        if remaining <= timedelta():
            status = "expired"
        elif remaining <= timedelta(days=14):
            warning = "The GitHub token expires within 14 days."
    return {"status": status, "expires": expires, "warning": warning,
            "checkedAt": record.get("checkedAt"), "repository": REPOSITORY}


def connection_test(token):
    checked = datetime.now(timezone.utc).isoformat()
    try:
        result = gh_request(BASE, token=token, include=True, timeout=15)
        headers, _, body = result.stdout.partition("\n\n")
        if result.returncode:
            status = ("rejected" if "HTTP 401" in result.stderr else
                      "access-denied" if re.search(r"HTTP (403|404)\b", result.stderr) else "unavailable")
            return {"status": status, "checkedAt": checked}
        repo = json.loads(body)
        if not is_private_repo(repo):
            return {"status": "access-denied", "checkedAt": checked}
        expiry = re.search(r"^github-authentication-token-expiration: (.+)$", headers, re.I | re.M)
        expires = None
        if expiry:
            value = expiry[1].strip()
            try:
                try:
                    date = datetime.fromisoformat(value.removesuffix(" UTC").replace("Z", "+00:00"))
                except ValueError:
                    date = parsedate_to_datetime(value)
                expires = date.replace(tzinfo=date.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError, OverflowError):
                pass
        return {"status": "valid", "expires": expires, "checkedAt": checked}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return {"status": "unavailable", "checkedAt": checked}


def manage_credential(action, candidate=None):
    saved, token = credential()
    if action == "status" or (action == "test" and not token):
        return credential_status(saved, token)
    if action == "replace":
        if not isinstance(candidate, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,512}", candidate):
            raise ValueError("Enter the GitHub token on one line.")
        token = candidate
    elif action != "test":
        raise ValueError("Invalid GitHub credential action.")
    result = connection_test(token)
    if action == "replace":
        if result["status"] != "valid":
            return {**credential_status(result, token), "saved": False}
        sha = os.environ.get(SHA_ENV) or saved.get("sha", "")
        github_credentials.save({**result, "sha": sha, "token": token})
        return {**credential_status(result, token), "saved": True}
    record = {**saved, **result}
    if saved:
        github_credentials.save(record)
    return credential_status(record, token)


def fetch(path, destination, limit, *, runner=False):
    cmd = ["gh", "api", "--hostname", "github.com", "-H", cloud.API_VERSION,
           "-H", "Accept: application/octet-stream", path]
    with destination.open("xb") as out, subprocess.Popen(
        cmd, env=gh_env(runner), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    ) as proc:
        try:
            copy_pipe(proc.stdout, out, limit, 180)
            if proc.wait(timeout=10):
                raise ValueError("Private media download was interrupted.")
        finally:
            if proc.poll() is None:
                proc.kill()


def is_private_repo(repo):
    return (repo["full_name"].casefold() == REPOSITORY and repo["visibility"] == "private"
            and repo["private"] is True and not repo["archived"] and not repo["disabled"])


def check_repo(*, runner=False):
    repo = api(BASE, runner=runner)
    if not is_private_repo(repo):
        raise ValueError("Production requires the designated active private repository.")
    return repo


def renderer_api(path, *, data=None):
    method = "POST" if data is not None else "GET"
    result = cloud.gh("api", "--hostname", "github.com", "-H", cloud.API_VERSION,
                      "-X", method, f"{RENDER_BASE}{path}",
                      *(("--input", "-") if data is not None else ()), data=data)
    return json.loads(result) if result else None


def renderer_pin(cfg):
    sha = cfg.get("githubRendererSha", "")
    if cfg.get("publicRepo", "").casefold() != RENDERER or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Pin the reviewed public production workflow before uploading.")
    return sha


def preflight(sha, renderer_sha):
    repo = check_repo()
    commit = api(f"{BASE}/commits/{quote(repo['default_branch'], safe='')}")
    private_flag = api(f"{BASE}/actions/variables/VRS_PRODUCTION_ENABLED", missing=True)
    if commit["sha"] != sha or (private_flag and private_flag["value"].casefold() == "true"):
        raise ValueError("Verify the private data commit and keep private rendering disabled.")
    renderer = renderer_api("")
    branch = renderer["default_branch"]
    if (branch != "main" or renderer["full_name"].casefold() != RENDERER or renderer["visibility"] != "public"
            or renderer["private"] or renderer["archived"] or renderer["disabled"]):
        raise ValueError("Production capture requires the designated active public renderer.")
    workflow = renderer_api(f"/actions/workflows/{WORKFLOW}")
    pinned = renderer_api(f"/commits/{quote(branch, safe='')}")
    if (pinned["sha"] != renderer_sha or workflow["state"] != "active"
            or workflow["path"] != f".github/workflows/{WORKFLOW}"):
        raise ValueError("Approve the deployed public production workflow commit before uploading.")
    flag = renderer_api("/actions/variables/VRS_HYBRID_PRODUCTION_SHA")
    if flag.get("value") != renderer_sha:
        raise ValueError("Enable hybrid production only after authorization, secure transfer and quota checks.")
    return branch, workflow["id"]


def tag(request):
    if not re.fullmatch(r"\d{14}-[a-f0-9]{8}", request):
        raise ValueError("Invalid production request.")
    return f"vrs-input-{request}"


def release(request, sha, descriptor):
    name = tag(request)
    # A failed create response can leave a draft. Recover only the exact request.
    drafts = api(f"{BASE}/releases?per_page=100")
    found = [item for item in drafts if item["tag_name"] == name]
    if len(found) > 1:
        raise ValueError("Ambiguous private input release. Inspect it before retrying.")
    item = found[0] if found else api(f"{BASE}/releases", method="POST", data={
        "tag_name": name, "target_commitish": sha, "name": name, "draft": True,
        "body": json.dumps(descriptor, sort_keys=True),
    })
    if (not item["draft"] or item["target_commitish"] != sha
            or json.loads(item.get("body") or "null") != descriptor):
        raise ValueError("Private input release does not match the approved commit.")
    return item


def erase_release(release_id, request, *, runner=False):
    item = api(f"{BASE}/releases/{int(release_id)}", missing=True, runner=runner)
    if item:
        if not item["draft"] or item["tag_name"] != tag(request):
            raise ValueError("Unexpected private draft release. Automatic deletion stopped.")
        api(f"{BASE}/releases/{int(release_id)}", method="DELETE", missing=True, runner=runner)


def find_run(receipt):
    if receipt.get("run"):
        candidates = [renderer_api(f"/actions/runs/{int(receipt['run'])}")]
    else:
        candidates = renderer_api(f"/actions/workflows/{receipt['workflow']}/runs?event=workflow_dispatch&per_page=100")["workflow_runs"]
    runs = [run for run in candidates
            if run["display_title"] == f"VRS Production {receipt['request']}"
            and run["head_sha"] == receipt["rendererSha"] and run["head_branch"] == receipt["branch"]
            and run["event"] == "workflow_dispatch" and run["workflow_id"] == receipt["workflow"]
            and run.get("head_repository", {}).get("full_name", "").casefold() == RENDERER]
    if len(runs) > 1 or (receipt.get("run") and not runs):
        raise ValueError("GitHub run does not uniquely match the saved production request.")
    return runs[0] if runs else None


def receive(receipt, run, root, limit):
    draft = api(f"{BASE}/releases/{int(receipt['release'])}")
    if not draft["draft"] or draft["tag_name"] != tag(receipt["request"]) or draft["target_commitish"] != receipt["sha"]:
        raise ValueError("Private output draft does not match the verified render request.")
    assets = [asset for asset in draft["assets"] if asset["name"] == DELIVERY and asset["state"] == "uploaded"]
    if len(assets) != 1 or not 0 < assets[0]["size"] <= limit:
        raise ValueError("Private production output is missing or exceeds the byte limit.")
    asset = assets[0]
    archive = root / DELIVERY
    fetch(f"{BASE}/releases/assets/{asset['id']}", archive, limit)
    if asset.get("digest") != "sha256:" + hash_file(archive):
        raise ValueError("Private production output archive checksum mismatch.")
    output = root / "final.mp4"
    with ZipFile(archive) as zipped:
        if sorted(zipped.namelist()) != ["final.mp4", "render-metrics.json"]:
            raise ValueError("Unexpected private production artifact files.")
        if zipped.getinfo("render-metrics.json").file_size > 65536:
            raise ValueError("Production metadata exceeds the byte limit.")
        metrics = json.loads(zipped.read("render-metrics.json"))
        with zipped.open("final.mp4") as src, output.open("xb") as dst:
            copy_bounded(src, dst, limit)
    if (metrics["run"] != run["id"] or metrics["manifest"] != receipt["manifest"] or metrics["preset"] != receipt["preset"]
            or metrics["inputSha"] != receipt["inputSha"] or metrics["frames"] != receipt["frames"]
            or metrics["sha256"] != hash_file(output)):
        raise ValueError("Production delivery does not match the approved input or its checksum.")
    return output, metrics


def runner_fetch(job, request):
    check_repo(runner=True)
    name = tag(request)
    matches = [draft for draft in api(f"{BASE}/releases?per_page=100", runner=True)
               if draft["draft"] and draft["tag_name"] == name]
    if len(matches) != 1:
        raise ValueError("Private input draft is unavailable.")
    item = matches[0]
    descriptor = json.loads(item["body"])
    checksum, manifest, preset = (descriptor[key] for key in ("inputSha", "manifest", "preset"))
    valid_id(checksum)
    valid_id(manifest)
    assets = item["assets"]
    if (descriptor["sha"] != item["target_commitish"]
            or descriptor["rendererSha"] != os.environ["GITHUB_SHA"]
            or len(assets) != 1 or assets[0]["name"] != ASSET
            or assets[0]["digest"] != "sha256:" + checksum
            or assets[0]["state"] != "uploaded" or not 0 < assets[0]["size"] <= LIMIT):
        raise ValueError("Private input release does not match the production dispatch.")
    asset_id = assets[0]["id"]
    job.mkdir(mode=0o700)
    archive = job / ASSET
    fetch(f"{BASE}/releases/assets/{int(asset_id)}", archive, LIMIT, runner=True)
    if hash_file(archive) != checksum:
        raise ValueError("Private production input checksum mismatch.")
    data = extract_package(archive, job / "input", LIMIT, manifest)
    if (data["operation"] != "render" or data["preset"] != preset
            or not {"voice.wav", "captions.ass"} <= data["files"].keys()):
        raise ValueError("Private production package is missing required render inputs.")
    (job / "manifest.json").write_text(json.dumps(data))
    archive.unlink()
    (job / "transfer.json").write_text(json.dumps({
        "release": item["id"], "inputSha": checksum, "manifest": manifest,
        "request": request, "sha": descriptor["sha"], "run": int(os.environ["GITHUB_RUN_ID"]),
    }))


def runner_render(job):
    from cloud_worker import PRESETS
    from remote_worker import execute, output, read

    transfer = read(job / "transfer.json")
    data = read(job / "manifest.json")
    preset = data["preset"]
    (job / "scratch").mkdir()
    execute(job, production_audio=True)
    final = job / "result"
    fps = PRESETS[preset][2]
    events = {phase: next(e["time"] for e in read(job / "events.json") if e["stage"] == phase)
              for phase in ("capture", "encode", "verify")}
    frames = int(output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=nb_frames", "-of", "csv=p=0", str(final)]).strip())
    delivery = job / "delivery"
    delivery.mkdir()
    final.rename(delivery / "final.mp4")
    metrics = dict(manifest=transfer["manifest"], inputSha=transfer["inputSha"], run=transfer["run"],
                   preset=preset, frames=frames,
                   duration=frames/fps, sha256=hash_file(delivery / "final.mp4"),
                   captureSeconds=events["encode"]-events["capture"],
                   encodingSeconds=events["verify"]-events["encode"], cpus=os.cpu_count(),
                   chrome=output(["/opt/google/chrome/chrome", "--version"]).strip(),
                   ffmpeg=output(["ffmpeg", "-version"]).splitlines()[0])
    (delivery / "render-metrics.json").write_text(json.dumps(metrics))


def runner_upload(job):
    check_repo(runner=True)
    transfer = json.loads((job / "transfer.json").read_text())
    release_id = int(transfer["release"])
    draft = api(f"{BASE}/releases/{release_id}", runner=True)
    if (not draft["draft"] or draft["tag_name"] != tag(transfer["request"])
            or draft["target_commitish"] != transfer["sha"]
            or [asset["name"] for asset in draft["assets"]] != [ASSET]):
        raise ValueError("Private output release changed.")
    delivery = job / "delivery"
    archive = job / DELIVERY
    if delivery.is_symlink() or not delivery.is_dir():
        raise ValueError("Private output directory is invalid.")
    mp4, metrics = (delivery / name for name in ("final.mp4", "render-metrics.json"))
    for path, maximum in ((mp4, LIMIT), (metrics, 65536)):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= maximum:
            raise ValueError("Private output file is invalid.")
    with ZipFile(archive, "x", compression=ZIP_STORED) as zipped:
        zipped.write(mp4, mp4.name)
        zipped.write(metrics, metrics.name)
    if not 0 < archive.stat().st_size <= LIMIT:
        raise ValueError("Production output exceeds the byte limit.")
    result = api(f"https://uploads.github.com/{BASE}/releases/{release_id}/assets?name={DELIVERY}",
                 method="POST", upload=archive, runner=True)
    if (result["state"] != "uploaded" or result["size"] != archive.stat().st_size
            or result.get("digest") != "sha256:" + hash_file(archive)):
        raise ValueError("Private output upload did not verify.")


def runner_probe(job):
    """Exercise private draft transfer using a fixed public-safe marker."""
    repo = check_repo(runner=True)
    run = str(int(os.environ["GITHUB_RUN_ID"]))
    attempt = str(int(os.environ["GITHUB_RUN_ATTEMPT"]))
    name = f"vrs-transfer-check-{run}-{attempt}"
    marker = job / "marker.txt"
    payload = b"VRS transfer check; no media.\n"
    marker.write_bytes(payload)
    draft = api(f"{BASE}/releases", method="POST", runner=True, data={
        "tag_name": name, "name": name, "target_commitish": repo["default_branch"], "draft": True,
    })
    release_id = int(draft["id"])
    if draft["tag_name"] != name or not draft["draft"]:
        raise ValueError("Probe draft was not private.")
    try:
        url = f"https://uploads.github.com/{BASE}/releases/{release_id}/assets?name=marker.txt"
        asset = api(url, method="POST", upload=marker, runner=True)
        asset_path = f"{BASE}/releases/assets/{int(asset['id'])}"
        if asset["digest"] != "sha256:" + hash_file(marker):
            raise ValueError("Probe upload digest mismatch.")
        download = job / "marker.download"
        fetch(asset_path, download, 1024, runner=True)
        if download.read_bytes() != payload:
            raise ValueError("Probe marker download mismatch.")
        anonymous = Request(f"https://api.github.com/{asset_path}",
                            headers={"Accept": "application/octet-stream"})
        try:
            with urlopen(anonymous, timeout=15):
                raise ValueError("Anonymous private asset access was allowed.")
        except HTTPError as error:
            if error.code not in (401, 403, 404):
                raise
    finally:
        api(f"{BASE}/releases/{release_id}", method="DELETE", runner=True)
        if api(f"{BASE}/releases/{release_id}", missing=True, runner=True) is not None:
            raise ValueError("Probe draft cleanup did not verify.")


if __name__ == "__main__":
    mode, folder, *args = sys.argv[1:]
    try:
        if mode == "fetch":
            runner_fetch(Path(folder), *args)
        elif mode == "render":
            runner_render(Path(folder), *args)
        elif mode == "upload":
            runner_upload(Path(folder))
        elif mode == "probe":
            runner_probe(Path(folder))
        else:
            raise ValueError("Unsupported hybrid workflow operation.")
    except Exception:
        # Private paths, FFmpeg output and rendered project content stay out of Actions logs.
        sys.exit("Hybrid production step failed.")
