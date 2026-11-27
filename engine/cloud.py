"""Optional GitHub Actions capture. Local CLI remains the default."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from time import monotonic, sleep
from urllib.parse import quote

from cloud_worker import FIXTURES, PRESETS, probe
from production_plan import CACHE, ENGINE, ROOT, digest, load, save
from production_render import render, stage


from studio_paths import APP_ROOT

FONT_NAMES = (
    "Poppins-Regular.ttf",
    "Poppins-Bold.ttf",
    "Poppins-OFL.txt",
    "Hind-Regular.ttf",
    "Hind-Bold.ttf",
    "Hind-OFL.txt",
)
WORKFLOW = ".github/workflows/vrs-cloud-render.yml"
DEPLOY = {WORKFLOW: APP_ROOT / WORKFLOW,
          **{f".github/vrs-cloud/{name}": ENGINE / name for name in
             ("cloud_worker.py", "package.json", "package-lock.json")},
          **{f".github/vrs-cloud/fonts/{name}": APP_ROOT / "installers" / "fonts" / name for name in FONT_NAMES}}
STATE = CACHE / "cloud"
API_VERSION = "X-GitHub-Api-Version: 2026-03-10"
PRODUCTION_BLOCK = (
    "GitHub-hosted production is disabled. Configure the verified private repository SHA and "
    "scoped transfer credential, or choose explicit Local execution."
)


class GitHubRejected(ValueError):
    """GitHub definitively refused the API request."""


def gh_env():
    env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
    for key in ("GH_DEBUG","GH_TOKEN","GITHUB_TOKEN","GH_ENTERPRISE_TOKEN","GITHUB_ENTERPRISE_TOKEN","GH_HOST"):
        env.pop(key, None)
    return env


def gh(*args, data=None, allowed=()):
    try:
        result = subprocess.run(["gh", *args], input=json.dumps(data) if data is not None else None,
                                text=True, capture_output=True, env=gh_env(), timeout=90)
    except subprocess.TimeoutExpired as error:
        raise ValueError("GitHub request timed out. Check status before retrying a dispatch.") from error
    if result.returncode:
        if any(f"HTTP {status}" in result.stderr for status in allowed):
            return None
        message = result.stderr.lower()
        if any(term in message for term in ("rate limit", "quota", "billing", "http 429", "spending limit")):
            raise GitHubRejected("GitHub reported a rate, quota or billing limit. Check Actions usage or retry later.")
        if any(term in message for term in ("http 401", "gh auth login", "bad credentials")):
            raise GitHubRejected("GitHub authentication failed. Check the selected account with gh auth status.")
        code = re.search(r"HTTP [45]\d\d", result.stderr)
        if code and code[0] in {"HTTP 403", "HTTP 404", "HTTP 422"}:
            raise GitHubRejected(f"GitHub CLI request failed ({code[0]}). Check repository, workflow and permissions.")
        raise ValueError("GitHub CLI request failed. Check connectivity and authentication.")
    return result.stdout


def api(path, data=None, method=None, missing=False):
    options = ["--hostname","github.com","-H", API_VERSION]
    if method:
        options += ["-X", method]
    if data is not None:
        options += ["--input", "-"]
    response = gh("api", *options, path, data=data, allowed=(404,) if missing else ())
    return json.loads(response) if response else None


def repository(args):
    for slug in (args.repo, args.private_repo):
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", slug):
            raise ValueError("Use GitHub OWNER/REPOSITORY slugs.")
    if args.repo.casefold() == args.private_repo.casefold():
        raise ValueError("Choose distinct source and renderer repositories.")
    account = api("user")["login"]
    if args.account and args.account.casefold() != account.casefold():
        raise ValueError("Active GitHub CLI account differs. Select it with gh auth switch.")
    repo = api(f"repos/{args.repo}")
    private = api(f"repos/{args.private_repo}")
    if repo["archived"] or repo["disabled"] or repo["visibility"] != "public" or repo["private"]:
        raise ValueError("The renderer repository must be public and active.")
    if private["visibility"] != "private" or private["private"] is not True:
        raise ValueError("The studio source repository must be private.")
    return account, repo, private


def commit(repo, branch=None):
    ref=quote(branch or repo["default_branch"],safe="")
    return api(f'repos/{repo["full_name"]}/commits/{ref}')["sha"]


def deployed(repo, sha=None, workflow=WORKFLOW):
    if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.(?:yml|yaml)",workflow):
        raise ValueError("Invalid workflow path.")
    result = []
    for target, local in DEPLOY.items():
        if target==WORKFLOW:
            target=workflow
        suffix = f"?ref={sha}" if sha else ""
        remote = api(f'repos/{repo["full_name"]}/contents/{target}{suffix}', missing=True)
        if remote is None:
            result.append((target, local))
        elif base64.b64decode(remote.get("content", "")) != local.read_bytes():
            raise ValueError(f"Remote deployment file differs: {target}. Review it before replacement.")
    return result


def install(repo):
    if not repo.get("permissions", {}).get("push"):
        raise ValueError("Repository write permission is required to add workflow files.")
    missing = deployed(repo)
    if any(path == WORKFLOW for path, _ in missing):
        headers = gh("api", "--include", "user").split("\n\n", 1)[0]
        scopes = re.search(r"^x-oauth-scopes:\s*([^\r\n]*)", headers, re.M | re.I)
        if scopes and scopes[1].strip() and "workflow" not in scopes[1].replace(" ", "").split(","):
            raise ValueError("Active OAuth session lacks the workflow scope. Authorize it with gh auth refresh -h github.com -s workflow.")
    # Add workflow after its dependencies so a partially installed setup is undispatchable.
    for path, local in sorted(missing, key=lambda pair: pair[0] == WORKFLOW):
        api(f'repos/{repo["full_name"]}/contents/{path}', method="PUT", data={
            "message": f"Add VRS cloud renderer: {Path(path).name}",
            "branch": repo["default_branch"], "content": base64.b64encode(local.read_bytes()).decode("ascii")})
    return {"installed": [path for path, _ in missing], "repository": repo["full_name"]}


def find_run(receipt):
    repo = receipt["repo"]
    def matches(run):
        return (run["display_title"] == f'VRS {receipt["id"]}' and run["head_sha"] == receipt["sha"]
                and run["event"] == "workflow_dispatch"
                and run.get("head_repository", {}).get("full_name") == repo
                and run.get("head_branch") == receipt.get("branch", run.get("head_branch"))
                and run.get("workflow_id") == receipt.get("workflow", run.get("workflow_id")))
    if receipt.get("run"):
        run = api(f'repos/{repo}/actions/runs/{receipt["run"]}')
        if not matches(run):
            raise ValueError("GitHub run differs from the saved synthetic job.")
        return run
    workflow=receipt.get("workflow",Path(WORKFLOW).name)
    result = api(f"repos/{repo}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=100")
    return next((run for run in result["workflow_runs"] if matches(run)), None)


def submit(args, account, repo, private):
    branch=args.branch or repo["default_branch"]
    sha = commit(repo,branch)
    fixture = getattr(args, "fixture", None)
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or args.preset not in PRESETS:
        raise ValueError("Invalid synthetic job revision or preset.")
    if fixture is not None and fixture not in FIXTURES:
        raise ValueError("Invalid synthetic fixture.")
    workflow=api(f'repos/{repo["full_name"]}/actions/workflows/{quote(args.workflow,safe="")}')
    if deployed(repo, sha,workflow["path"]):
        raise ValueError("The reviewed synthetic workflow is not installed on the selected branch.")
    duration_sec = 12 if fixture == "premium-showcase" else 2
    digest_parts = [repo["full_name"], private["full_name"], branch, workflow["id"], sha, args.preset, "synthetic-smoke-v1"]
    if fixture is not None:
        digest_parts.append(fixture)
    request = digest(*digest_parts)
    path = STATE / f"{request}.json"
    STATE.mkdir(parents=True, exist_ok=True)
    receipt = {"id": request, "repo": repo["full_name"], "privateRepo": private["full_name"],
               "account": account, "sha": sha, "branch":branch,"workflow":workflow["id"],
               "preset": args.preset, "frames": duration_sec * PRESETS[args.preset][2],
               **({"fixture": fixture} if fixture is not None else {}),
               "manifest": {"version": 1, "operation": "synthetic-smoke", "jobId": request,
                            "inputRevision": sha, "preset": args.preset},
               "created": datetime.now(timezone.utc).isoformat()}
    try:
        with path.open("x", encoding="utf-8") as file:
            json.dump(receipt, file, indent=2)
    except FileExistsError:
        return {"state": "existing", **load(path)}
    inputs = {"request_id": request, "preset": args.preset, "expected_sha": sha}
    if fixture is not None:
        inputs["fixture"] = fixture
    try:
        response = api(f'repos/{repo["full_name"]}/actions/workflows/{workflow["id"]}/dispatches',
                       method="POST", data={"ref": branch, "inputs": inputs})
    except GitHubRejected:
        path.unlink()
        raise
    except (OSError, ValueError) as error:
        raise ValueError(f"{error} Saved request {request}. Check its status before attempting another dispatch.") from error
    if response and response.get("workflow_run_id"):
        receipt["run"] = response["workflow_run_id"]
        save(path, receipt)
    return receipt


def seconds(start, end):
    if start and end:
        return (datetime.fromisoformat(end.replace("Z", "+00:00"))
                - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    return None


def status(receipt, path):
    job = find_run(receipt)
    elapsed = seconds(receipt.get("created"), datetime.now(timezone.utc).isoformat()) or 0
    if not job:
        return {"status": "awaiting-index", "timedOut": elapsed > 900,
                "message": "Inspect the GitHub dispatch before retrying."}
    if not receipt.get("run"):
        receipt["run"] = job["id"]
        save(path, receipt)
    steps = api(f'repos/{receipt["repo"]}/actions/runs/{job["id"]}/jobs')["jobs"]
    capture = next((step for step in steps if step["name"] == "capture"), {})
    completed = job["status"] == "completed"
    return {"run": job["id"], "status": job["status"], "conclusion": job["conclusion"],
            "timedOut": not completed and elapsed > 900,
            "url": f'https://github.com/{receipt["repo"]}/actions/runs/{job["id"]}',
            "queueSeconds": seconds(job["created_at"], capture.get("started_at")),
            "jobSeconds": seconds(capture.get("started_at"), capture.get("completed_at")),
            "steps": [{"name": step["name"], "status": step["status"]} for step in capture.get("steps", [])],
            "failedSteps": [step["name"] for step in capture.get("steps", []) if step["conclusion"] == "failure"]}


def download(receipt, path):
    run = status(receipt, path)
    if run.get("conclusion") != "success":
        raise ValueError(f"Cloud capture unavailable. {run}")
    target = STATE / receipt["id"]
    if not target.is_dir():
        with tempfile.TemporaryDirectory(dir=STATE) as folder:
            temp = Path(folder) / "download"
            started = monotonic()
            gh("run", "download", str(run["run"]), "--repo", receipt["repo"], "--name", "visual-master",
               "--dir", str(temp))
            elapsed = monotonic() - started
            if {p.name for p in temp.iterdir()} != {"visual-only.mp4", "render-metrics.json"}:
                raise ValueError("Unexpected artifact contents.")
            local = probe(temp / "visual-only.mp4", receipt["preset"], receipt["frames"])
            remote = load(temp / "render-metrics.json")
            if any(remote.get(key) != value for key, value in local.items()):
                raise ValueError("The downloaded master differs from runner measurements.")
            save(temp / "download-metrics.json", {**run, "downloadSeconds": elapsed, "render": remote})
            temp.rename(target)
    expected = load(target / "download-metrics.json")["render"]["sha256"]
    if hashlib.sha256((target / "visual-only.mp4").read_bytes()).hexdigest() != expected:
        raise ValueError("Cached downloaded master does not match its verified hash.")
    return target


def finish(target, receipt):
    master = target / "visual-only.mp4"
    final = target / "finalized-smoke.mp4"
    if not final.is_file():
        with tempfile.TemporaryDirectory(dir=target) as folder:
            temp = Path(folder) / final.name
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", master, "-map", "0:v:0",
                            "-c:v", "copy", "-movflags", "+faststart", temp], check=True)
            probe(temp, receipt["preset"], receipt["frames"])
            temp.replace(final)
    return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Public renderer OWNER/REPO")
    parser.add_argument("--private-repo", required=True, help="Private studio source OWNER/REPO")
    parser.add_argument("--account", help="Require this active GitHub CLI account")
    parser.add_argument("--branch", help="Branch for the public synthetic workflow")
    parser.add_argument("--workflow", default=Path(WORKFLOW).name, help="Workflow filename or ID")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("install", help="Publish only the four isolated workflow files to the selected repository")
    new = commands.add_parser("submit")
    new.add_argument("--preset", choices=PRESETS, default="draft")
    new.add_argument("--fixture", choices=FIXTURES, default=None)
    for action in ("status", "watch", "download", "finish"):
        sub = commands.add_parser(action)
        sub.add_argument("request")
    args = parser.parse_args()
    try:
        account, repo, private = repository(args)
        if args.command == "check":
            branch=args.branch or repo["default_branch"]
            workflow=api(f'repos/{repo["full_name"]}/actions/workflows/{quote(args.workflow,safe="")}')
            result = {"account": account, "repo": repo["full_name"], "visibility": repo["visibility"],
                      "privateRepo": private["full_name"], "privateVisibility": private["visibility"],
                      "branch":branch,"workflow":workflow["id"],
                      "missing": [path for path, _ in deployed(repo,commit(repo,branch),workflow["path"])]}
        elif args.command == "install":
            result = install(repo)
        elif args.command == "submit":
            result = submit(args, account, repo, private)
        else:
            if not re.fullmatch(r"[0-9a-f]{20}", args.request):
                raise ValueError("Invalid request ID.")
            path = STATE / f"{args.request}.json"
            receipt = load(path)
            if (receipt["repo"], receipt.get("privateRepo"), receipt["account"]) != (repo["full_name"], private["full_name"], account):
                raise ValueError("Receipt belongs to a different repository or GitHub CLI account.")
            if args.command in {"status", "watch"}:
                result = status(receipt, path)
                if args.command == "watch":
                    for _ in range(8):
                        if result["status"] == "completed":
                            break
                        sleep(5)
                        result = status(receipt, path)
            else:
                target = download(receipt, path)
                result = load(target / "download-metrics.json")
                result["directory"] = str(target)
                if args.command == "finish":
                    result["finalized"] = str(finish(target, receipt))

        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Cloud operation stopped: {error}\n")


if __name__ == "__main__":
    main()
