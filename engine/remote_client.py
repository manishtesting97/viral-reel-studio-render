"""Local and strict OpenSSH transports for the independent production worker."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

from remote_protocol import (MANIFEST_LIMIT, copy_pipe, extract_package, hash_file, job_id,
                             reserve_output, valid_id, write_package)
from remote_protocol import DEFAULT_LIMITS


class Local:
    def __init__(self, state, limits=None):
        self.command = [sys.executable, str(Path(__file__).with_name("remote_worker.py")), "--state", str(state)]
        if limits:
            self.command += ["--limits", str(limits)]

    def request(self, args, **kwargs):
        return subprocess.run([*self.command,*args],check=True,timeout=120,**kwargs)


def ssh_options(config):
    if os.name == "nt":
        raise ValueError("The pinned independent worker transport currently requires Linux.")
    host,user,port=config["host"],config["user"],config.get("port",22)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}",host) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}",user):
        raise ValueError("Invalid host or SSH username.")
    if type(port) is not int or not 1<=port<=65535:
        raise ValueError("Invalid SSH port.")
    identity=Path(config["key"]).expanduser().resolve(strict=True)
    hosts=Path(config["known_hosts"]).expanduser().resolve(strict=True)
    if not identity.is_file() or identity.stat().st_mode & 0o077 or not hosts.is_file() or not hosts.stat().st_size:
        raise ValueError("Supply a private SSH key and a populated pinned known_hosts file.")
    if any(c in str(hosts) for c in ('"', '\\', '\n', '\r', '%', '$')):
        raise ValueError("Unsupported known_hosts path.")
    return (["-F","/dev/null","-o","StrictHostKeyChecking=yes","-o",f'UserKnownHostsFile="{hosts}"',
             "-o","GlobalKnownHostsFile=/dev/null","-o","UpdateHostKeys=no","-o","BatchMode=yes",
             "-o","IdentitiesOnly=yes","-o","IdentityAgent=none","-o","PreferredAuthentications=publickey",
             "-o","ForwardAgent=no","-o","ClearAllForwardings=yes","-o","ConnectTimeout=10",
             "-o","ServerAliveInterval=15","-o","ServerAliveCountMax=2","-i",str(identity)],
            f"{user}@{host}",str(port))


class SSH:
    def __init__(self, config):
        self.options,self.destination,self.port=ssh_options(config)
        self.exchange=ssh_options(config["sftp"]) if "sftp" in config else None

    def request(self,args,**kwargs):
        for token in args[1:]:
            valid_id(token)
        if args[0] not in {"capabilities","status","submit","import","cancel","fetch","publish","ack","reap"}:
            raise ValueError("Unsupported SSH operation.")
        return subprocess.run(["ssh","-T","-p",self.port,*self.options,self.destination,*args],
                              check=True,timeout=180,**kwargs)

    def upload(self, ident, checksum, stream, size, progress):
        with subprocess.Popen(["ssh","-T","-p",self.port,*self.options,self.destination,
                               "submit",valid_id(ident),valid_id(checksum)],
                              stdin=stream,stdout=subprocess.PIPE) as process:
            deadline=time.monotonic()+180
            try:
                while process.poll() is None:
                    progress(stream.tell(),size)
                    if time.monotonic()>deadline:
                        raise ValueError("SSH upload timed out; retry to recover the receipt.")
                    time.sleep(.25)
                reply=process.stdout.read(MANIFEST_LIMIT+1)
                if process.returncode or len(reply)>MANIFEST_LIMIT:
                    raise ValueError("SSH upload was interrupted or rejected; inspect job status before retrying.")
                return json.loads(reply)
            finally:
                if process.poll() is None:
                    process.kill()

    def sftp(self, action, local, remote, byte_limit=None):
        options,destination,port=self.exchange
        local=str(local.resolve())
        if any(c in local for c in ('"', '\\', '\n','\r')):
            raise ValueError("Unsupported SFTP local path.")
        batch=f'{action} "{local}" "{remote}"\n' if action=="put" else f'get "{remote}" "{local}"\n'
        def limit_file():
            import resource
            resource.setrlimit(resource.RLIMIT_FSIZE,(byte_limit,byte_limit))

        subprocess.run(["sftp","-b","-","-P",port,*options,destination],input=batch.encode(),
                       stdout=subprocess.DEVNULL,check=True,timeout=180,
                       preexec_fn=limit_file if byte_limit is not None else None)


def rpc(transport, *args, stream=None):
    kwargs={"stdin":stream} if stream is not None else {"stdin":subprocess.DEVNULL}
    return json.loads(transport.request(args,stdout=subprocess.PIPE,**kwargs).stdout)


def submit(transport, package, limit, progress=None):
    if package.stat().st_size>limit:
        raise ValueError("Archive exceeds client byte limit.")
    with ZipFile(package) as archive:
        info=archive.getinfo("manifest.json")
        if info.file_size>MANIFEST_LIMIT:
            raise ValueError("Manifest too large.")
        ident=job_id(json.loads(archive.read(info)))
    result=rpc(transport,"status",ident)
    if result["state"]!="missing":
        return result
    # Validate locally before any transport transmits the selected package.
    with tempfile.TemporaryDirectory() as temp:
        extract_package(package,Path(temp)/"input",limit,ident)
    checksum=hash_file(package)
    if isinstance(transport,SSH) and transport.exchange:
        transport.sftp("put",package,f"incoming/{ident}.zip")
        return rpc(transport,"import",ident,checksum)
    with package.open("rb") as stream:
        if isinstance(transport,SSH) and progress:
            return transport.upload(ident,checksum,stream,package.stat().st_size,progress)
        return rpc(transport,"submit",ident,checksum,stream=stream)


def fetch(transport, ident, destination, limit, revision=None, progress=None):
    valid_id(ident)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination already exists.")
    state=rpc(transport,"status",ident)
    if state["state"]!="succeeded":
        raise ValueError("Job has no downloadable result.")
    if revision is not None and state.get("revision")!=valid_id(revision):
        raise ValueError("Result does not match the current input revision.")
    record=state["output"]
    valid_id(record["sha256"])
    if type(record["bytes"]) is not int or not 0<record["bytes"]<=limit:
        raise ValueError("Result exceeds the client byte limit.")
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        partial=Path(temp)/"result.part"
        if isinstance(transport,SSH) and transport.exchange:
            rpc(transport,"publish",ident)
            transport.sftp("get",partial,f"results/{ident}.result",record["bytes"])
        else:
            # A stream cap is checked before any bytes beyond the advertised limit are saved.
            with tempfile.TemporaryFile() as stderr:
                command=(*transport.command,"fetch",ident) if isinstance(transport,Local) else (
                    "ssh","-T","-p",transport.port,*transport.options,transport.destination,"fetch",ident)
                with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=stderr,
                                      stdin=subprocess.DEVNULL) as child:
                    try:
                        with partial.open("xb") as target:
                            copy_pipe(child.stdout,target,record["bytes"],180,progress)
                        if child.wait(timeout=180):
                            raise ValueError("Download interrupted; retry fetch for the same job ID.")
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.wait()
        if partial.stat().st_size!=record["bytes"] or hash_file(partial)!=record["sha256"]:
            raise ValueError("Output checksum or size mismatch.")
        reserve_output(partial,destination)
    return {"id":ident,"path":str(destination),"bytes":record["bytes"],"sha256":record["sha256"]}


def validate_studio_bundle(project, plan, base, timing, dark, standard, approved):
    """Match a review bundle against the canonical stage and supported caption tracks."""
    from production_render import subtitles

    preview_html = (approved / "index.html").read_text(encoding="utf-8")
    # Review bundles add these playback layers to the standard stage.
    preview_html = re.sub(r'<video id="editorial-captions"[^>]*></video>\n?', "", preview_html)
    preview_html = re.sub(r'<audio id="editorial-narration"[^>]*></audio>\n?', "", preview_html)
    if (preview_html != (standard / "index.html").read_text(encoding="utf-8")
            or hash_file(approved / "assets" / "voice.wav") != hash_file(project / "voice.wav")
            or any(hash_file(approved / path.relative_to(standard)) != hash_file(path)
                   for path in (standard / "assets").rglob("*") if path.is_file())):
        raise ValueError("The approved Studio bundle differs from the current scenes, assets or narration.")
    preview_caption = (approved / "captions.ass").read_bytes()
    subtitles(standard, plan, base["words"], dark_scenes=dark, project=project)
    if (standard / "captions.ass").read_bytes() != preview_caption:
        subtitles(standard, plan, timing["words"], dark_scenes=dark, project=project)
    if (standard / "captions.ass").read_bytes() != preview_caption:
        raise ValueError("The approved Studio caption track differs from both saved timeline variants.")


def pack_project(slug, source, package, preset, limit, *, production_audio=False, approved_preview=None):
    from caption_review import require_caption_timing
    from production_plan import PROJECTS, SCHEMA, current_alignment_report, digest, load, save, validate
    from production_render import caption_settings, profiles, source_files, stage, subtitles

    schema=load(SCHEMA)
    validate(slug,schema["properties"]["id"],"project")
    project=PROJECTS/slug
    plan=load(project/"plan.json")
    validate(plan,schema)
    voice=project/"voice.wav"
    timing,report=require_caption_timing(project,plan)
    timeline=project/"timeline.json"
    raw_report=current_alignment_report(voice,plan)
    base=load(timeline) if timeline.is_file() and raw_report.get("status") != "failed" else timing
    cues = [cue for scene in timing["scenes"] for cue in scene["audio_cues"]]
    tracks = [(cue, project / "tracks" / f'{cue["id"]}.wav') for cue in cues]
    tracks = [(cue, path) for cue, path in tracks if path.is_file()]
    if tracks and not production_audio:
        raise ValueError("Use local production for projects with separate audio tracks in protocol V1.")
    if caption_settings(*profiles(plan, project=project)).get("highlighter"):
        try:
            import PIL.ImageFont
        except ImportError as error:
            raise ValueError("Pillow is required for the configured caption highlighter.") from error
    with tempfile.TemporaryDirectory() as tmp:
        from asset_library import saved
        build=stage(Path(tmp),plan,timing,source.resolve(),preset,asset_project=project)
        dark={sid for sid,path in source_files(source,plan,saved(project))[0].items()
              if "k8s-scene--dark" in path.read_text(encoding="utf-8")}
        if approved_preview is not None:
            standard = stage(Path(tmp), plan, timing, source.resolve(), "standard", asset_project=project)
            validate_studio_bundle(project, plan, base, timing, dark, standard, approved_preview)
            if build != standard:
                shutil.copyfile(standard / "captions.ass", build / "captions.ass")
        else:
            subtitles(build, plan, timing["words"], dark_scenes=dark, project=project)
        shutil.copyfile(voice,build/"voice.wav")
        if production_audio and tracks:
            audio = build / "assets" / "vrs-production-audio"
            audio.mkdir()
            for index, (_, path) in enumerate(tracks):
                if path.is_symlink():
                    raise ValueError("Audio tracks must not be symlinked.")
                shutil.copyfile(path, audio / f"{index}.wav")
            save(audio / "cues.json", [{"kind": cue["kind"], "at": cue["at"]} for cue, _ in tracks])
        revision=hash_file(voice)+digest(plan,timing,report,build.name)
        revision=hashlib.sha256(revision.encode()).hexdigest()
        return write_package(package,build,"render",revision,limit,preset)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-bytes",type=int,default=DEFAULT_LIMITS["max_transfer"])
    group=parser.add_mutually_exclusive_group()
    group.add_argument("--local",type=Path)
    group.add_argument("--ssh",type=Path)
    parser.add_argument("--limits",type=Path,help="Local transport test limits")
    commands=parser.add_subparsers(dest="command",required=True)
    pack=commands.add_parser("pack")
    pack.add_argument("operation",choices=("transcribe","render"))
    pack.add_argument("root",type=Path)
    pack.add_argument("package",type=Path)
    pack.add_argument("--revision",required=True)
    pack.add_argument("--preset",choices=("draft","standard","premium"))
    pack.add_argument("--language",choices=("en","hi","hinglish","auto"))
    pack.add_argument("--reviewed",action="store_true")
    project=commands.add_parser("pack-project")
    project.add_argument("project")
    project.add_argument("source",type=Path)
    project.add_argument("package",type=Path)
    project.add_argument("--preset",choices=("draft","standard","premium"),required=True)
    project.add_argument("--reviewed",required=True,action="store_true")
    commands.add_parser("capabilities")
    commands.add_parser("reap")
    submit_cmd=commands.add_parser("submit")
    submit_cmd.add_argument("package",type=Path)
    submit_cmd.add_argument("--authorize-transfer",action="store_true")
    for name in ("status","cancel","ack","fetch"):
        command=commands.add_parser(name)
        command.add_argument("id",type=valid_id)
        if name=="fetch":
            command.add_argument("destination",type=Path)
            command.add_argument("--revision",type=valid_id,required=True)
    args=parser.parse_args()
    try:
        if args.max_bytes<1:
            raise ValueError("Invalid client byte limit.")
        if args.command=="pack-project":
            manifest=pack_project(args.project,args.source,args.package,args.preset,args.max_bytes)
            result={"id":job_id(manifest),"package":str(args.package),"revision":manifest["revision"]}
        elif args.command=="pack":
            if args.operation=="render" and not args.reviewed:
                raise ValueError("Review the executable stage and supplied assets first.")
            manifest=write_package(args.package,args.root,args.operation,args.revision,args.max_bytes,args.preset,args.language)
            result={"id":job_id(manifest),"package":str(args.package)}
        else:
            if not (args.local or args.ssh):
                raise ValueError("Choose --local or --ssh.")
            transport=Local(args.local,args.limits) if args.local else SSH(json.loads(args.ssh.read_bytes()))
            if args.command=="submit":
                if args.ssh and not args.authorize_transfer:
                    raise ValueError("Explicitly authorize sending this package to the configured worker.")
                result=submit(transport,args.package,args.max_bytes)
            elif args.command=="fetch":
                result=fetch(transport,args.id,args.destination,args.max_bytes,args.revision)
            else:
                result=rpc(transport,args.command,*([args.id] if hasattr(args,"id") else []))
        print(json.dumps(result,indent=2))
    except (OSError,ValueError,KeyError,TypeError,ImportError,subprocess.SubprocessError) as error:
        parser.exit(1,f"Remote client stopped: {error}\n")


if __name__=="__main__":
    main()
