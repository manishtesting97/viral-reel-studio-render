"""Private GitHub production transport. The public synthetic contract is separate."""

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import stat
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

import cloud
import github_credentials
from remote_protocol import MANIFEST_LIMIT, MAX_FILES, copy_bounded, copy_pipe, extract_package, hash_file, safe_path, valid_id

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
RUNNER_ENV = "VRS_GITHUB_RUNNER"
RUNNER_MODES = ("auto", "self-hosted", "github-hosted")
HOSTED_WORKLOADS = {"software-project", "agreement-covered"}
_EXECUTION_CONTEXT = {}


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


def renderer_api(path, *, data=None, missing=False):
    method = "POST" if data is not None else "GET"
    result = cloud.gh("api", "--hostname", "github.com", "-H", cloud.API_VERSION,
                      "-X", method, f"{RENDER_BASE}{path}",
                      *(("--input", "-") if data is not None else ()), data=data,
                      allowed=(404,) if missing and method == "GET" else ())
    return json.loads(result) if result else None


def renderer_pin(cfg):
    sha = cfg.get("githubRendererSha", "")
    if cfg.get("publicRepo", "").casefold() != RENDERER or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Pin the reviewed public production workflow before uploading.")
    return sha


def _online_self_hosted_runners(runners_payload):
    runners = (runners_payload or {}).get("runners") if isinstance(runners_payload, dict) else None
    matched = []
    for runner in runners or ():
        if not isinstance(runner, dict) or str(runner.get("status", "")).casefold() != "online":
            continue
        labels = {
            str(lbl.get("name", "")).strip().casefold()
            for lbl in (runner.get("labels") or ())
            if isinstance(lbl, dict) and lbl.get("name")
        }
        os_name = str(runner.get("os", "")).casefold()
        if labels and "self-hosted" not in labels:
            continue
        if "linux" not in labels and os_name != "linux":
            continue
        if ({"arm", "arm64", "aarch64", "windows", "macos"} & labels) or (os_name and os_name != "linux"):
            continue
        matched.append(runner)
    return matched


def _verify_repo_state():
    repo = check_repo()
    commit = api(f"{BASE}/commits/{quote(repo['default_branch'], safe='')}")
    private_flag = api(f"{BASE}/actions/variables/VRS_PRODUCTION_ENABLED", missing=True)
    if private_flag and private_flag["value"].casefold() == "true":
        raise ValueError("Keep private repository Actions execution disabled (`VRS_PRODUCTION_ENABLED`).")
    renderer = renderer_api("")
    branch = renderer["default_branch"]
    if (branch != "main" or renderer["full_name"].casefold() != RENDERER or renderer["visibility"] != "public"
            or renderer["private"] or renderer["archived"] or renderer["disabled"]):
        raise ValueError("Production capture requires the designated active public renderer.")
    workflow = renderer_api(f"/actions/workflows/{WORKFLOW}")
    pinned = renderer_api(f"/commits/{quote(branch, safe='')}")
    if workflow["state"] != "active" or workflow["path"] != f".github/workflows/{WORKFLOW}":
        raise ValueError("Activate the reviewed public production workflow before uploading.")
    environment = renderer_api("/environments/vrs-hybrid-production")
    if environment.get("can_admins_bypass") is not False:
        raise ValueError("Disable administrator bypass on the protected production environment before uploading.")
    branches = renderer_api("/environments/vrs-hybrid-production/deployment-branch-policies")["branch_policies"]
    if {policy["name"] for policy in branches} != {"main"}:
        raise ValueError("Restrict the protected production environment to the main branch.")
    return {
        "privateSha": commit["sha"],
        "rendererSha": pinned["sha"],
        "branch": branch,
        "workflow": workflow["id"],
    }


def _select_runner(runner_preference="auto"):
    pref = (runner_preference or os.environ.get(RUNNER_ENV) or "auto").strip().casefold() or "auto"
    if pref not in RUNNER_MODES:
        raise ValueError("Choose Auto, Self-hosted, or GitHub-hosted for runner selection.")
    runners_payload = renderer_api("/actions/runners", missing=True)
    self_hosted = _online_self_hosted_runners(runners_payload)
    if pref == "self-hosted":
        if not self_hosted:
            raise ValueError(
                f"Self-hosted runner mode is selected, but no online Linux x64 self-hosted runner is registered on {RENDERER}. "
                "Start an authorized self-hosted runner or switch runner selection to Auto or GitHub-hosted."
            )
        return "self-hosted", self_hosted, None
    if pref == "auto" and self_hosted:
        return "self-hosted", self_hosted, None
    workload = renderer_api("/actions/variables/VRS_HYBRID_WORKLOAD", missing=True)
    raw_workload = str((workload or {}).get("value", "")).strip()
    classification = raw_workload.partition(":")[0].strip().casefold()
    if classification in HOSTED_WORKLOADS:
        return "github-hosted", self_hosted, classification
    if classification == "content-production":
        raise ValueError(
            f"GitHub-hosted standard runners do not permit routine non-software content production under GitHub Actions Terms "
            f"(VRS_HYBRID_WORKLOAD=content-production), and zero online Linux x64 self-hosted runners are available on {RENDERER}. "
            "Register an authorized self-hosted runner, update VRS_HYBRID_WORKLOAD if running an eligible software-project or agreement-covered workload, "
            "or explicitly choose Local execution."
        )
    raise ValueError(
        f"No online Linux x64 self-hosted runner is registered on {RENDERER}, and VRS_HYBRID_WORKLOAD is not set to "
        "software-project or agreement-covered for GitHub-hosted runners. "
        "Register an authorized self-hosted runner, configure an eligible hosted workload classification, or explicitly choose Local execution."
    )


def synchronize_pins(cfg, *, config_path=None, compatibility_check=None):
    saved, token = credential()
    if cfg.get("privateRepo", "").casefold() != REPOSITORY or cfg.get("publicRepo", "").casefold() != RENDERER or not token:
        raise ValueError(cloud.PRODUCTION_BLOCK)
    state = _verify_repo_state()
    private_sha = os.environ.get(SHA_ENV) or state["privateSha"]
    renderer_sha = state["rendererSha"]
    if not re.fullmatch(r"[a-f0-9]{40}", private_sha) or not re.fullmatch(r"[a-f0-9]{40}", renderer_sha):
        raise ValueError("Could not resolve verified 40-character commits from GitHub.")
    if private_sha != state["privateSha"]:
        raise ValueError("Configured VRS_GITHUB_PRODUCTION_SHA does not match the current private default branch commit.")
    if compatibility_check is not None:
        compatibility_check(renderer_sha)
    changed = (cfg.get("githubProductionSha") != private_sha or cfg.get("githubRendererSha") != renderer_sha)
    cfg["githubProductionSha"] = private_sha
    cfg["githubRendererSha"] = renderer_sha
    if changed and config_path is not None:
        cfg_file = Path(config_path)
        current = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
        if current.get("githubProductionSha") != private_sha or current.get("githubRendererSha") != renderer_sha:
            current["githubProductionSha"] = private_sha
            current["githubRendererSha"] = renderer_sha
            cfg_file.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    if saved and saved.get("token") and saved.get("sha") != private_sha and not os.environ.get(SHA_ENV):
        try:
            github_credentials.save({**saved, "sha": private_sha})
        except ValueError:
            pass
    return private_sha, renderer_sha


def last_runner():
    return _EXECUTION_CONTEXT.get("runner", "github-hosted")


def execution_status(cfg, *, compatibility_check=None):
    try:
        sha, renderer_sha = synchronize_pins(dict(cfg), compatibility_check=compatibility_check)
        runner, self_hosted, classification = _select_runner(cfg.get("githubRunner") or "auto")
        return {
            "available": True,
            "runner": runner,
            "selfHostedOnline": len(self_hosted),
            "workload": classification or "self-hosted",
            "githubProductionSha": sha,
            "githubRendererSha": renderer_sha,
            "reason": None,
        }
    except Exception as error:
        return {
            "available": False,
            "runner": None,
            "selfHostedOnline": 0,
            "workload": None,
            "githubProductionSha": cfg.get("githubProductionSha"),
            "githubRendererSha": cfg.get("githubRendererSha"),
            "reason": str(error),
        }


def preflight(sha, renderer_sha, *, runner_preference="auto"):
    state = _verify_repo_state()
    if state["privateSha"] != sha:
        raise ValueError("Private repository commit changed. Retry so pins synchronize with the verified default branch.")
    if state["rendererSha"] != renderer_sha:
        raise ValueError("Public renderer commit changed. Retry so pins synchronize with the verified default branch.")
    runner, _, _ = _select_runner(runner_preference)
    _EXECUTION_CONTEXT["runner"] = runner
    return state["branch"], state["workflow"]


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


def _download_delivery(receipt, root, limit):
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
    return archive


def receive(receipt, run, root, limit):
    archive = _download_delivery(receipt, root, limit)
    output = root / "final.mp4"
    companions = (
        ("visual-only.mp4", "visualOnlySha256"),
        ("captioned-silent.mp4", "captionedSilentSha256"),
        ("captions.ass", "captionsAssSha256"),
    )
    allowed = {"final.mp4", "render-metrics.json", *(name for name, _ in companions)}
    with ZipFile(archive) as zipped:
        names = set(zipped.namelist())
        if len(zipped.infolist()) != len(names) or not {"final.mp4", "render-metrics.json"} <= names or not names <= allowed:
            raise ValueError("Unexpected private production artifact files.")
        for info in zipped.infolist():
            file_type = stat.S_IFMT(info.external_attr >> 16)
            if (info.filename != info.orig_filename or info.is_dir() or file_type not in (0, stat.S_IFREG)
                    or info.flag_bits & 1):
                raise ValueError("Only unencrypted regular ZIP entries are permitted.")
        if zipped.getinfo("render-metrics.json").file_size > 65536:
            raise ValueError("Production metadata exceeds the byte limit.")
        metrics = json.loads(zipped.read("render-metrics.json"))
        with zipped.open("final.mp4") as src, output.open("xb") as dst:
            copy_bounded(src, dst, limit)
        if (metrics["run"] != run["id"] or metrics["manifest"] != receipt["manifest"] or metrics["preset"] != receipt["preset"]
                or metrics["inputSha"] != receipt["inputSha"] or metrics["frames"] != receipt["frames"]
                or metrics["sha256"] != hash_file(output)):
            raise ValueError("Production delivery does not match the approved input or its checksum.")
        for name, digest_key in companions:
            if name in names:
                dest = root / name
                with zipped.open(name) as src, dest.open("xb") as dst:
                    copy_bounded(src, dst, limit)
                if metrics.get(digest_key) != hash_file(dest):
                    raise ValueError("Production companion file checksum mismatch.")
            elif digest_key in metrics:
                raise ValueError("Expected private production companion file is missing.")
    return output, metrics


def receive_transcription(receipt, run, root, limit):
    archive = _download_delivery(receipt, root, limit)
    transcript = root / "transcript.json"
    with ZipFile(archive) as zipped:
        if sorted(zipped.namelist()) != ["transcribe-metrics.json", "transcript.json"]:
            raise ValueError("Unexpected private transcription artifact files.")
        if zipped.getinfo("transcribe-metrics.json").file_size > 65536:
            raise ValueError("Transcription metadata exceeds the byte limit.")
        metrics = json.loads(zipped.read("transcribe-metrics.json"))
        with zipped.open("transcript.json") as src, transcript.open("xb") as dst:
            copy_bounded(src, dst, limit)
    if (metrics.get("run") != run["id"] or metrics.get("manifest") != receipt["manifest"]
            or metrics.get("inputSha") != receipt["inputSha"] or metrics.get("operation") != "transcribe"
            or metrics.get("revision") != receipt["revision"] or metrics.get("sha256") != hash_file(transcript)):
        raise ValueError("Transcription delivery does not match the approved input or its checksum.")
    return transcript, metrics


def receive_preparation(receipt, run, root, limit):
    archive = _download_delivery(receipt, root, limit)
    prepared = root / "prepared"
    prepared.mkdir()
    with ZipFile(archive) as zipped:
        inventory = zipped.infolist()
        if len(inventory) > MAX_FILES + 2 or len({info.filename for info in inventory}) != len(inventory):
            raise ValueError("Unexpected private preparation archive inventory.")
        for info in inventory:
            safe_path(info.filename)
            file_type = stat.S_IFMT(info.external_attr >> 16)
            if (info.filename != info.orig_filename or info.is_dir() or file_type not in (0, stat.S_IFREG)
                    or info.flag_bits & 1 or info.compress_type not in (ZIP_STORED, ZIP_DEFLATED)):
                raise ValueError("Only unencrypted regular ZIP entries are permitted.")
        for name, maximum in (("prepare-metrics.json", 65536), ("prepare-manifest.json", MANIFEST_LIMIT)):
            if name not in zipped.namelist() or zipped.getinfo(name).file_size > maximum:
                raise ValueError("Preparation metadata is missing or exceeds the byte limit.")
        metrics = json.loads(zipped.read("prepare-metrics.json"))
        manifest_bytes = zipped.read("prepare-manifest.json")
        files = json.loads(manifest_bytes)
        if not isinstance(files, dict) or len(files) > MAX_FILES:
            raise ValueError("Invalid scene preparation manifest.")
        if set(zipped.namelist()) != (set(files) | {"prepare-metrics.json", "prepare-manifest.json"}):
            raise ValueError("Unexpected private preparation artifact files.")
        import hashlib
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        if (metrics.get("run") != run["id"] or metrics.get("manifest") != receipt["manifest"]
                or metrics.get("inputSha") != receipt["inputSha"] or metrics.get("operation") != "prepare"
                or metrics.get("revision") != receipt["revision"] or metrics.get("project") != receipt["project"]
                or not metrics.get("validation", {}).get("ok")
                or metrics.get("manifestSha256") != manifest_sha):
            raise ValueError("Scene preparation delivery does not match the approved input or its checksum.")
        for name, record in files.items():
            safe_path(name)
            if (not isinstance(record, dict) or set(record) != {"bytes", "sha256"}
                    or type(record["bytes"]) is not int or record["bytes"] < 0):
                raise ValueError("Invalid scene preparation file record.")
            valid_id(record["sha256"])
            info = zipped.getinfo(name)
            if info.file_size != record["bytes"]:
                raise ValueError("Scene preparation file size mismatch.")
            dest = prepared / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(info) as src, dest.open("xb") as out:
                copy_bounded(src, out, record["bytes"])
            if dest.stat().st_size != record["bytes"] or hash_file(dest) != record["sha256"]:
                raise ValueError("Scene preparation file checksum mismatch.")
    return prepared, metrics


def runner_fetch(job, request, expected_operation=None, expected_recovery=None, *, metadata_only=False):
    check_repo(runner=True)
    name = tag(request)
    matches = [draft for draft in api(f"{BASE}/releases?per_page=100", runner=True)
               if draft["draft"] and draft["tag_name"] == name]
    if len(matches) != 1:
        raise ValueError("Private input draft is unavailable.")
    item = matches[0]
    descriptor = json.loads(item["body"])
    operation = descriptor.get("operation", "render")
    checksum, manifest, preset = descriptor["inputSha"], descriptor["manifest"], descriptor.get("preset")
    valid_id(checksum)
    valid_id(manifest)
    assets = item["assets"]
    if (descriptor["sha"] != item["target_commitish"]
            or descriptor["rendererSha"] != os.environ["GITHUB_SHA"]
            or len(assets) != 1 or assets[0]["name"] != ASSET
            or assets[0]["digest"] != "sha256:" + checksum
            or assets[0]["state"] != "uploaded" or not 0 < assets[0]["size"] <= LIMIT):
        raise ValueError("Private input release does not match the production dispatch.")
    if operation not in {"transcribe", "prepare", "render"}:
        raise ValueError("Unsupported hybrid workflow operation.")
    recovery = descriptor.get("recovery", False)
    if type(recovery) is not bool or (recovery and operation != "transcribe"):
        raise ValueError("Invalid recovery mode.")
    if metadata_only:
        print(f"operation={operation}\nrecovery={str(recovery).lower()}")
        return
    if expected_operation is not None and (operation, str(recovery).lower()) != (expected_operation, expected_recovery):
        raise ValueError("Private draft operation changed after prerequisite selection.")
    asset_id = assets[0]["id"]
    job.mkdir(mode=0o700)
    archive = job / ASSET
    fetch(f"{BASE}/releases/assets/{int(asset_id)}", archive, LIMIT, runner=True)
    if hash_file(archive) != checksum:
        raise ValueError("Private production input checksum mismatch.")
    data = extract_package(archive, job / "input", LIMIT, manifest)
    if operation == "render":
        if (data["operation"] != "render" or data["preset"] != preset
                or not {"voice.wav", "captions.ass"} <= data["files"].keys()):
            raise ValueError("Private production package is missing required render inputs.")
    elif operation == "transcribe":
        if (data["operation"] != "transcribe" or preset is not None or data["preset"] is not None
                or data["language"] != descriptor.get("language")
                or data["revision"] != descriptor.get("revision")
                or "voice.wav" not in data["files"]
                or ("recovery.json" in data["files"]) != recovery
                or any(name not in {"voice.wav", "recovery.json"} for name in data["files"])):
            raise ValueError("Private production package is missing required transcription inputs.")
    elif operation == "prepare":
        slug = descriptor.get("project")
        if (data["operation"] != "prepare" or preset is not None or data["preset"] is not None
                or data["language"] is not None or data["revision"] != descriptor.get("revision")
                or not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", slug)
                or f"projects/{slug}/plan.json" not in data["files"]):
            raise ValueError("Private production package is missing required scene preparation inputs.")
    else:
        raise ValueError("Unsupported hybrid workflow operation.")
    (job / "manifest.json").write_text(json.dumps(data))
    archive.unlink()
    (job / "transfer.json").write_text(json.dumps({
        "release": item["id"], "inputSha": checksum, "manifest": manifest,
        "operation": operation, "request": request, "sha": descriptor["sha"],
        "run": int(os.environ["GITHUB_RUN_ID"]),
        **({"revision": data["revision"]} if operation in {"transcribe", "prepare"} else {}),
        **({"project": descriptor["project"], "mode": descriptor.get("mode"),
            "sceneId": descriptor.get("sceneId"), "force": bool(descriptor.get("force"))}
           if operation == "prepare" else {}),
    }))


def _valid_word_tokens(rows, duration, *, offset=0.0):
    import math
    words = []
    for row in rows:
        if isinstance(row, dict):
            text, a, b, p = row.get("text") or row.get("word") or "", row.get("start"), row.get("end"), row.get("probability")
        else:
            text, a, b, p = getattr(row, "word", "") or "", getattr(row, "start", None), getattr(row, "end", None), getattr(row, "probability", None)
        try:
            start, end, prob = round(offset + float(a), 3), round(offset + float(b), 3), float(0.0 if p is None else p)
        except (TypeError, ValueError):
            continue
        if (text.strip() and math.isfinite(start) and math.isfinite(end) and math.isfinite(prob)
                and 0.0 <= start < end <= duration + 0.02 and (end - start) >= 0.03 and 0.0 < prob <= 1.0):
            if words and start < words[-1]["end"]:
                start = round(words[-1]["end"], 3)
            if start < end and (end - start) >= 0.03:
                words.append({"text": text.strip(), "start": start, "end": min(end, duration), "probability": prob})
    return words


def _apex_recovery(decoded, duration, language, recovery_spec, models_dir):
    import stable_whisper

    apex_dir = models_dir / "apex-ct2-int8"
    if not (apex_dir / "model.bin").is_file():
        return None
    t0 = time.monotonic()
    apex_model = stable_whisper.load_faster_whisper(
        str(apex_dir), device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True,
    )
    whisper_lang = "en" if language == "hinglish" else {"en": "en", "hi": "hi"}.get(language) or "en"
    raw_segments, info = apex_model.transcribe_original(
        str(decoded), language=whisper_lang, beam_size=3, word_timestamps=True, condition_on_previous_text=False,
    )
    apex_rows = []
    for seg in raw_segments:
        for w in (seg.words or []):
            apex_rows.append({"text": w.word, "start": w.start, "end": w.end, "probability": w.probability})
    apex_words = _valid_word_tokens(apex_rows, duration)

    forced_words = []
    scenes = recovery_spec.get("scenes") or []
    narration = " ".join(str(s.get("narration", "")).strip() for s in scenes if str(s.get("narration", "")).strip())
    if narration:
        aligned = apex_model.align(
            str(decoded), narration, language=whisper_lang, original_split=False,
            fast_mode=True, suppress_silence=False, remove_instant_words=True, verbose=None,
        )
        forced_words = _valid_word_tokens(aligned.all_words(), duration)

    # Start with continuous forced-aligned tokens, then fill uncovered spans from native Apex ASR (trimming small overlaps).
    clean_words = list(forced_words)
    for aw in apex_words:
        ts, te = aw["start"], aw["end"]
        for ew in clean_words:
            if te <= ew["start"] + 1e-4 or ts >= ew["end"] - 1e-4:
                continue
            if ts < ew["start"] <= te and (ew["start"] - ts) >= 0.04:
                te = min(te, ew["start"])
            elif ts <= ew["end"] < te and (te - ew["end"]) >= 0.04:
                ts = max(ts, ew["end"])
            else:
                te = ts
                break
        if te - ts >= 0.04:
            clean_words.append({**aw, "start": round(ts, 3), "end": round(te, 3)})
    clean_words.sort(key=lambda w: w["start"])

    recognition = {
        "configuredLanguage": whisper_lang,
        "language": getattr(info, "language", whisper_lang) or whisper_lang,
        "languageProbability": float(getattr(info, "language_probability", 1.0) or 1.0),
    }
    del apex_model
    return {
        "model": "apex-ct2-int8",
        "recognition": recognition,
        "windows": recovery_spec.get("windows") or [[0.0, round(duration, 2), True]],
        "words": clean_words,
        "alternate": apex_words,
        "apexWords": apex_words,
        "forcedWords": forced_words,
        "apexSeconds": round(time.monotonic() - t0, 3),
    }


def runner_transcribe(job):
    from alignment import DEVANAGARI
    from production_plan import load_whisper_model, probe_duration, run_whisper, whisper_model_name
    from remote_worker import output, read
    from studio_paths import MODELS

    transfer = read(job / "transfer.json")
    manifest = read(job / "manifest.json")
    folder = job / "input"
    scratch = job / "scratch"
    scratch.mkdir()
    decoded = scratch / "decoded.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-f", "wav", "-i", str(folder / "voice.wav"),
         "-vn", "-ac", "1", "-ar", "16000", str(decoded)],
        check=True,
    )
    duration = probe_duration(decoded)
    language = manifest["language"]
    started = time.monotonic()
    recovery_file = folder / "recovery.json"
    apex_payload = (_apex_recovery(decoded, duration, language, read(recovery_file), MODELS)
                    if recovery_file.is_file() else None)
    if apex_payload is not None:
        model_name = "apex-ct2-int8"
        words = apex_payload["apexWords"]
        payload = {
            "language": apex_payload["recognition"]["language"],
            "model": model_name,
            "revision": manifest["revision"],
            "recognition": apex_payload["recognition"],
            "words": words,
            "recovery": {k: v for k, v in apex_payload.items() if k != "recognition"},
        }
    else:
        model_name = whisper_model_name(language, MODELS)
        whisper_lang = None if language == "hinglish" else {"en": "en", "hi": "hi"}.get(language)
        model = load_whisper_model(model_name, MODELS)
        words, recognition = run_whisper(model, decoded, whisper_lang, beam_size=3, condition_on_previous_text=False)
        words = [w for w in words if w["end"] <= duration + 0.02]
        payload = {
            "language": recognition["language"],
            "model": model_name,
            "revision": manifest["revision"],
            "recognition": recognition,
            "words": words,
        }
        rec_windows = manifest.get("windows")
        if isinstance(rec_windows, list) and rec_windows:
            from faster_whisper.audio import decode_audio
            sample_rate = 16000
            audio = decode_audio(str(decoded), sampling_rate=sample_rate)
            rec_prim, rec_alt = [], []
            for item in rec_windows[:10]:
                start, end, has_en = float(item[0]), float(item[1]), bool(item[2]) if len(item) > 2 else False
                left, right = max(0, round(start * sample_rate)), min(len(audio), round(end * sample_rate))
                if right <= left:
                    continue
                rw, _ = run_whisper(model, audio[left:right], whisper_lang, offset=left / sample_rate,
                                    beam_size=3, condition_on_previous_text=False)
                rec_prim.extend(w for w in rw if left / sample_rate <= w["start"] < w["end"] <= right / sample_rate)
                if has_en and language == "hinglish" and (end - start) <= 6.0:
                    rwe, _ = run_whisper(model, audio[left:right], "en", offset=left / sample_rate,
                                         beam_size=3, condition_on_previous_text=False)
                    rec_alt.extend(w for w in rwe if left / sample_rate <= w["start"] < w["end"] <= right / sample_rate)
            payload["recovery"] = {"windows": rec_windows[:10], "words": rec_prim, "alternate": rec_alt, "model": model_name}
        if (language == "hinglish" and model_name != "tiny.en" and duration <= 180
                and any(DEVANAGARI.search(w["text"]) for w in words)
                and (MODELS / "models--Systran--faster-whisper-tiny.en/refs/main").is_file()):
            del model
            witness = load_whisper_model("tiny.en", MODELS)
            alt_words, alt_info = run_whisper(witness, decoded, "en", beam_size=3, condition_on_previous_text=False)
            alt_info["witnessModel"] = "tiny.en"
            payload["alternate"] = [w for w in alt_words if w["end"] <= duration + 0.02]
            payload["alternateRecognition"] = alt_info
            del witness
        else:
            del model
    elapsed = round(time.monotonic() - started, 3)
    delivery = job / "delivery"
    delivery.mkdir()
    transcript = delivery / "transcript.json"
    transcript.write_text(json.dumps(payload), encoding="utf-8")
    metrics = {
        "manifest": transfer["manifest"],
        "inputSha": transfer["inputSha"],
        "run": transfer["run"],
        "operation": "transcribe",
        "revision": manifest["revision"],
        "model": model_name,
        "wordCount": len(words),
        "transcribeSeconds": elapsed,
        **({"apexWordCount": len(payload["recovery"].get("apexWords", [])),
            "forcedWordCount": len(payload["recovery"].get("forcedWords", [])),
            "apexSeconds": payload["recovery"].get("apexSeconds")}
           if isinstance(payload.get("recovery"), dict) and payload["recovery"].get("model") == "apex-ct2-int8" else {}),
        "sha256": hash_file(transcript),
        "ffmpeg": output(["ffmpeg", "-version"]).splitlines()[0],
    }
    (delivery / "transcribe-metrics.json").write_text(json.dumps(metrics), encoding="utf-8")


def _prepare_workspace(job):
    import hashlib
    import asset_library
    from caption_review import require_caption_timing
    from production_plan import PROJECTS, extract, load
    from production_render import scene_prompts, source_files, stage, subtitles
    import visual_generator

    transfer = json.loads((job / "transfer.json").read_text(encoding="utf-8"))
    slug = transfer["project"]
    project_dir = PROJECTS / slug
    plan = load(project_dir / "plan.json")
    timing, _ = require_caption_timing(project_dir, plan)
    started = time.monotonic()
    result = visual_generator.generate_visuals(
        project_dir,
        mode=transfer.get("mode"),
        scene_id=transfer.get("sceneId"),
        force=bool(transfer.get("force")),
    )
    if not result["validation"]["ok"]:
        raise ValueError("; ".join(result["validation"]["issues"][:3]))
    scene_prompts(project_dir, plan, timing)
    extract(project_dir, plan)
    source_dir = project_dir / "visuals"
    assignments = asset_library.saved(project_dir)
    staged = stage(project_dir, plan, timing, source_dir, "standard")
    dark = {sid for sid, path in source_files(source_dir, plan, assignments)[0].items()
            if "k8s-scene--dark" in path.read_text(encoding="utf-8")}
    subtitles(staged, plan, timing["words"], dark_scenes=dark, project=project_dir)
    elapsed = round(time.monotonic() - started, 3)

    delivery = job / "delivery"
    delivery.mkdir()
    entries = {}

    def record(src, rel):
        safe_path(rel)
        if src.is_symlink() or not src.is_file():
            raise ValueError("Prepared scene file must be a regular file.")
        dest = delivery / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        entries[rel] = {"bytes": dest.stat().st_size, "sha256": hash_file(dest)}

    for folder_name in ("visuals", "prompts"):
        base = project_dir / folder_name
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Prepared directory must not contain symlinks.")
                if path.is_file():
                    record(path, f"{folder_name}/{path.relative_to(base).as_posix()}")
    for filename in ("visual-config.json", "visual-production-prompt.txt"):
        candidate = project_dir / filename
        if candidate.is_file():
            record(candidate, filename)
    captions = staged / "captions.ass"
    if not captions.is_file():
        raise ValueError("Caption layout could not be prepared.")
    record(captions, "captions.ass")

    manifest_bytes = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    (delivery / "prepare-manifest.json").write_bytes(manifest_bytes)
    prepared_models = sum(scene["component"] == "procedural-model" for scene in result["generated"])
    active_models = sum(scene["component"] == "procedural-model" and scene["status"] == "auto-generated"
                        for scene in result["status"]["scenes"])
    metrics = {
        "manifest": transfer["manifest"],
        "inputSha": transfer["inputSha"],
        "run": transfer["run"],
        "operation": "prepare",
        "revision": transfer["revision"],
        "project": slug,
        "mode": transfer.get("mode"),
        "sceneId": transfer.get("sceneId"),
        "scenesGenerated": len(result["generated"]),
        "modelsRequired": sum("model" in scene.get("storyboard", {}) for scene in plan["scenes"]),
        "modelsPrepared": prepared_models,
        "modelsReused": max(0, active_models - prepared_models),
        "validation": result["validation"],
        "prepareSeconds": elapsed,
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    (delivery / "prepare-metrics.json").write_text(json.dumps(metrics), encoding="utf-8")


def runner_prepare(job):
    (job / "scratch" / "cache").mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "VRS_WORKSPACE": str(job / "input"),
        "VRS_CACHE_DIR": str(job / "scratch" / "cache"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "_prepare", str(job)], check=True, env=env)


def runner_execute(job):
    from remote_worker import read

    transfer = read(job / "transfer.json")
    manifest = read(job / "manifest.json")
    operation = transfer.get("operation") or manifest.get("operation")
    if operation == "transcribe":
        runner_transcribe(job)
    elif operation == "prepare":
        runner_prepare(job)
    elif operation == "render":
        runner_render(job)
    else:
        raise ValueError("Unsupported hybrid workflow operation.")


def runner_render(job):
    from cloud_worker import PRESETS
    from remote_worker import execute, output, read

    transfer = read(job / "transfer.json")
    data = read(job / "manifest.json")
    preset = data["preset"]
    (job / "scratch").mkdir()
    delivery = job / "delivery"
    delivery.mkdir()
    execute(job, production_audio=True, companion_dir=delivery)
    final = job / "result"
    fps = PRESETS[preset][2]
    events = {phase: next(e["time"] for e in read(job / "events.json") if e["stage"] == phase)
              for phase in ("capture", "encode", "verify")}
    frames = int(output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=nb_frames", "-of", "csv=p=0", str(final)]).strip())
    final.rename(delivery / "final.mp4")
    if (job / "input" / "captions.ass").is_file():
        shutil.copyfile(job / "input" / "captions.ass", delivery / "captions.ass")
    metrics = dict(
        manifest=transfer["manifest"], inputSha=transfer["inputSha"], run=transfer["run"],
        preset=preset, frames=frames, duration=frames/fps,
        sha256=hash_file(delivery / "final.mp4"),
        visualOnlySha256=hash_file(delivery / "visual-only.mp4"),
        captionedSilentSha256=hash_file(delivery / "captioned-silent.mp4"),
        **({"captionsAssSha256": hash_file(delivery / "captions.ass")} if (delivery / "captions.ass").is_file() else {}),
        captureSeconds=events["encode"]-events["capture"],
        encodingSeconds=events["verify"]-events["encode"], cpus=os.cpu_count(),
        chrome=output(["/opt/google/chrome/chrome", "--version"]).strip(),
        ffmpeg=output(["ffmpeg", "-version"]).splitlines()[0],
    )
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
    operation = transfer.get("operation", "render")
    if operation == "transcribe":
        files = [(delivery / name, name, maximum) for name, maximum in (("transcript.json", LIMIT), ("transcribe-metrics.json", 65536))]
    elif operation == "prepare":
        manifest_file = delivery / "prepare-manifest.json"
        if manifest_file.is_symlink() or not manifest_file.is_file() or manifest_file.stat().st_size > MANIFEST_LIMIT:
            raise ValueError("Preparation manifest is invalid.")
        entries = json.loads(manifest_file.read_text(encoding="utf-8"))
        if not isinstance(entries, dict) or len(entries) > MAX_FILES:
            raise ValueError("Preparation manifest is invalid.")
        files = [
            *((delivery / safe_path(name).as_posix(), name, LIMIT) for name in entries),
            (manifest_file, "prepare-manifest.json", MANIFEST_LIMIT),
            (delivery / "prepare-metrics.json", "prepare-metrics.json", 65536),
        ]
    elif operation == "render":
        files = [(delivery / name, name, maximum) for name, maximum in (("final.mp4", LIMIT), ("render-metrics.json", 65536))]
        for extra in ("visual-only.mp4", "captioned-silent.mp4", "captions.ass"):
            path = delivery / extra
            if path.is_file():
                files.append((path, extra, LIMIT))
    else:
        raise ValueError("Unsupported hybrid workflow operation.")
    for path, _arcname, maximum in files:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= maximum:
            raise ValueError("Private output file is invalid.")
    with ZipFile(archive, "x", compression=ZIP_STORED) as zipped:
        for path, arcname, _maximum in files:
            zipped.write(path, arcname)
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
        elif mode == "metadata":
            runner_fetch(Path(folder), *args, metadata_only=True)
        elif mode == "execute":
            runner_execute(Path(folder))
        elif mode == "_prepare":
            _prepare_workspace(Path(folder))
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
