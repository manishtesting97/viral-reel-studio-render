"""Direct, single-tenant Linux worker. GitHub Actions is not a transport."""
import argparse
from contextlib import contextmanager
import fcntl
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from zipfile import BadZipFile

from remote_protocol import (DEFAULT_LIMITS, VERSION, copy_bounded, copy_pipe, encode, extract_package, hash_file, valid_id)
from cloud_worker import PRESETS

ENGINE = Path(__file__).resolve().parent
TERMINAL = {"succeeded", "failed", "cancelled", "cleaned"}
PHASES = {"transcribe", "capture", "encode", "verify"}


def atomic(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(encode(value))
    os.replace(tmp.name, path)


def read(path):
    return json.loads(path.read_bytes())


def proc_birth(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except FileNotFoundError:
        return None


def bytes_used(folder):
    total=0
    for f in folder.rglob("*"):
        try:
            info=f.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            total+=info.st_size
    return total


def runtime_env():
    allowed = ("HOME", "PATH", "LANG", "LC_ALL", "VRS_REMOTE_BROWSER", "VRS_REMOTE_MODEL")
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    env.update(DO_NOT_TRACK="1", HYPERFRAMES_CHROME_SANDBOX="1", PRODUCER_RESTRICT_PAGE_NETWORK="1",
               PRODUCER_BROWSER_GPU_MODE="software", HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    browser = env.get("VRS_REMOTE_BROWSER")
    if browser:
        env["HYPERFRAMES_BROWSER_PATH"] = browser
    return env


def output(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=15)


def capability(root, limits):
    machine = platform.machine()
    ram = int(next(row.split()[1] for row in Path("/proc/meminfo").read_text().splitlines()
                   if row.startswith("MemTotal:"))) * 1024
    cpu = len(os.sched_getaffinity(0))
    # Respect standard cgroup-v2 ceilings when they are present for this process.
    group = next((row[3:] for row in Path("/proc/self/cgroup").read_text().splitlines()
                  if row.startswith("0::")), "")
    mount = Path("/sys/fs/cgroup")
    for path in (mount / group.lstrip("/"), *list((mount / group.lstrip("/")).parents)):
        if not path.is_relative_to(mount):
            continue
        if (path / "cpu.max").is_file():
            quota, period = (path / "cpu.max").read_text().split()
            if quota != "max":
                cpu = min(cpu, int(quota) / int(period))
        if (path / "memory.max").is_file():
            ceiling = (path / "memory.max").read_text().strip()
            if ceiling != "max":
                ram = min(ram, int(ceiling))
    disk = shutil.disk_usage(root).free
    dependencies = {}

    def probe(name, check):
        try:
            dependencies[name] = check()
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            dependencies[name] = None

    probe("node", lambda: output(["node", "--version"]).strip())
    probe("ffmpeg", lambda: output(["ffmpeg", "-version"]).splitlines()[0])
    probe("ffprobe", lambda: output(["ffprobe", "-version"]).splitlines()[0])
    probe("chrome", lambda: output([os.environ.get("VRS_REMOTE_BROWSER", ""), "--version"]).strip())
    probe("hyperframes", lambda: read(ENGINE / "node_modules/hyperframes/package.json")["version"])
    probe("three", lambda: read(ENGINE / "node_modules/three/package.json")["version"])
    probe("gsap", lambda: (ENGINE / "node_modules/hyperframes/dist/vendor/npm/gsap@3.15.0/dist/gsap.min.js").is_file())
    if dependencies.get("ffmpeg"):
        probe("encoders", lambda: all(re.search(r"\b"+codec+r"\b", output(["ffmpeg","-hide_banner","-encoders"]))
                                      for codec in ("libx264", "aac")))
        probe("ass", lambda: bool(re.search(r"\bass\b", output(["ffmpeg","-hide_banner","-filters"]))))
    try:
        from importlib.util import find_spec
        from importlib.metadata import PackageNotFoundError
        dependencies["faster-whisper"] = version("faster-whisper") if find_spec("faster_whisper") else None
        dependencies["ctranslate2"] = version("ctranslate2") if find_spec("ctranslate2") else None
        dependencies["av"] = version("av")
    except PackageNotFoundError:
        pass
    model = os.environ.get("VRS_REMOTE_MODEL", "")
    model_path = Path(model) if model else None
    model_ready = bool(model_path) and all((model_path/name).is_file() for name in ("model.bin","config.json","tokenizer.json"))
    model_family = None
    if model_ready:
        resolved = model_path.resolve()
        container = resolved.parent.parent.name if resolved.parent.name == "snapshots" else resolved.name
        model_family = container.removeprefix("models--Systran--faster-whisper-")
    ready = (platform.system()=="Linux" and machine in {"x86_64", "aarch64", "arm64"}
             and os.geteuid()!=0 and cpu>=limits["min_cpu"] and ram>=limits["min_ram"]
             and disk>=limits["min_disk"])
    node = re.match(r"v(\d+)", dependencies.get("node") or "")
    render = bool(ready and node and int(node[1])>=22 and dependencies.get("hyperframes")=="0.9.88"
                  and dependencies.get("three")=="0.170.0" and dependencies.get("gsap") and all(
                      dependencies.get(k) for k in ("chrome","ffmpeg","ffprobe","encoders","ass")))
    transcribe = bool(ready and model_ready and dependencies.get("ffprobe") and dependencies.get("ffmpeg")
                      and dependencies.get("faster-whisper")=="1.2.1" and dependencies.get("ctranslate2")
                      and dependencies.get("av")=="16.1.0")
    return dict(version=VERSION, architecture=machine, cpu=cpu, ramBytes=ram, diskFree=disk,
                directory=str(root.resolve()),model=Path(model).name if model_ready else None,
                modelFamily=model_family,multilingual=bool(model_family and not model_family.endswith(".en")),
                limits=limits, versions=dependencies, operations={"render":render,"transcribe":transcribe},
                webgl="requires synthetic runtime test")





class Worker:
    def __init__(self, root, limits=None, exchange=None):
        if os.geteuid()==0:
            raise ValueError("Run under a dedicated non-root user.")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077:
            raise ValueError("Worker state must be owned by the worker and private (0700).")
        self.root = root.resolve()
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        if (set(self.limits)!=set(DEFAULT_LIMITS) or any(type(v) is not int or v<1 for v in self.limits.values())
                or self.limits["max_storage"] < 2*self.limits["max_transfer"]):
            raise ValueError("Invalid worker limits.")
        self.exchange = exchange
        if exchange and (not exchange.is_absolute() or any(
            not (exchange/p).is_dir() or (exchange/p).is_symlink() for p in ("incoming","results")
        )):
            raise ValueError("SFTP exchange requires separate incoming and results directories.")

    @contextmanager
    def lock(self):
        with (self.root / "lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def path(self, ident):
        return self.root / valid_id(ident)

    def status(self, ident):
        job=self.path(ident)
        if not (job/"status.json").is_file():
            return {"id":ident,"state":"missing"}
        result=read(job/"status.json")
        if result["state"]=="running" and proc_birth(result["pid"]) != result["birth"]:
            child=job/"child.json"
            if child.is_file():
                record=read(child)
                if record["birth"] and record["birth"]==proc_birth(record["pid"]):
                    try:
                        os.killpg(record["pid"],signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            result.update(state="failed",reason="Worker process interrupted.",finished=time.time())
            self.finish(job,result)
        elif result["state"]=="queued":
            result.update(state="failed",reason="Submission interrupted before launch.",finished=time.time())
            self.finish(job,result)
        event=job/"progress.json"
        if event.is_file():
            result["progress"]=read(event)
        events=job/"events.json"
        if events.is_file():
            result["events"]=read(events)
        return result

    def finish(self, job, status):
        for part in ("input","scratch"):
            target=job/part
            if target.exists():
                shutil.rmtree(target)
        for name in ("manifest.json","config.json","child.json","cancel"):
            (job/name).unlink(missing_ok=True)
        if status["state"]!="succeeded":
            (job/"result").unlink(missing_ok=True)
        atomic(job/"status.json",status)

    def clean(self, job, status):
        (job/"result").unlink(missing_ok=True)
        for name in ("progress.json","events.json"):
            (job/name).unlink(missing_ok=True)
        if self.exchange:
            (self.exchange/"results"/(job.name+".result")).unlink(missing_ok=True)
        atomic(job/"status.json",dict(id=job.name,state="cleaned",finished=time.time()))

    def reap(self):
        for job in self.root.iterdir():
            # Transfers are serialized by the worker lock. Any abandoned
            # upload directory is safe to remove after the retention window.
            if (job.is_dir() and re.fullmatch(r"upload-[a-z0-9_]+",job.name)
                    and time.time()-job.stat().st_mtime>self.limits["retention_seconds"]):
                shutil.rmtree(job)
                continue
            if not job.is_dir() or not re.fullmatch(r"[0-9a-f]{64}",job.name):
                continue
            result=self.status(job.name)
            age=time.time()-result.get("finished",time.time())
            if result["state"]=="cleaned" and age>7*self.limits["retention_seconds"]:
                shutil.rmtree(job)
            elif result["state"] in TERMINAL-{"cleaned"} and age>self.limits["retention_seconds"]:
                self.clean(job,result)
        if self.exchange:
            for p in (self.exchange/"results").glob(".publish-*"):
                if time.time()-p.lstat().st_mtime>self.limits["retention_seconds"]:
                    p.unlink()
            for p in (self.exchange/"incoming").glob("*.zip"):
                if re.fullmatch(r"[0-9a-f]{64}\.zip",p.name) and (
                    time.time()-p.lstat().st_mtime>self.limits["retention_seconds"]
                ):
                    p.unlink()

    def submit(self, ident, archive_hash, stream, pipe=False):
        valid_id(archive_hash)
        job=self.path(ident)
        with self.lock():
            self.reap()
            old=self.status(ident)
            if old["state"]!="missing":
                return old
            jobs=[p for p in self.root.iterdir() if p.is_dir()]
            if len(jobs)>=500 or any(self.status(p.name)["state"]=="running" for p in jobs
                                     if re.fullmatch(r"[0-9a-f]{64}",p.name)):
                raise ValueError("Worker is busy or its job quota is exhausted.")
            used=bytes_used(self.root)
            reservation=min(self.limits["max_storage"]-used, shutil.disk_usage(self.root).free)
            if reservation<self.limits["max_transfer"]*2:
                raise ValueError("Insufficient worker storage.")
            with tempfile.TemporaryDirectory(prefix="upload-",dir=self.root) as temp:
                temp=Path(temp)
                archive=temp/"upload.zip"
                with archive.open("xb") as f:
                    if pipe:
                        copy_pipe(stream,f,self.limits["max_transfer"],150)
                    else:
                        copy_bounded(stream,f,self.limits["max_transfer"])
                if hash_file(archive)!=archive_hash:
                    raise ValueError("Archive checksum mismatch.")
                directory=temp/"job"
                directory.mkdir()
                manifest=extract_package(archive,directory/"input",self.limits["max_transfer"],ident)
                cap=capability(self.root,self.limits)
                if not cap["operations"][manifest["operation"]]:
                    raise ValueError("Worker preflight did not approve this operation.")
                atomic(directory/"manifest.json",manifest)
                atomic(directory/"config.json",{**self.limits,"max_storage":reservation})
                atomic(directory/"status.json",{"id":ident,"state":"queued"})
                directory.rename(job)
                try:
                    process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"_run",str(job)],
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         start_new_session=True,env=runtime_env())
                except OSError:
                    failed=dict(id=ident,state="failed",reason="Cannot start worker.",finished=time.time())
                    self.finish(job,failed)
                    return failed
                result=dict(id=ident,state="running",pid=process.pid,birth=proc_birth(process.pid) or "exited",
                            started=time.time(),revision=manifest["revision"])
                atomic(job/"status.json",result)
                return result

    def import_sftp(self, ident, checksum):
        if not self.exchange:
            raise ValueError("SFTP exchange is not configured.")
        file=self.exchange/"incoming"/(valid_id(ident)+".zip")
        fd=os.open(file,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,"rb") as src:
            if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                raise ValueError("SFTP upload is not a regular file.")
            result=self.submit(ident,checksum,src)
        file.unlink(missing_ok=True)
        return result


def run_job(job):
    worker=Worker(job.parent,read(job/"config.json"))
    with worker.lock():
        status=worker.status(job.name)
    if status["state"]!="running":
        return
    home=job/"scratch"
    home.mkdir(mode=0o700)
    model=os.environ.get("VRS_REMOTE_MODEL")
    if model:
        model=str(Path(model).resolve())
    env={**runtime_env(),"HOME":str(home),"TMPDIR":str(home),"XDG_CACHE_HOME":str(home),
         "HYPERFRAMES_CACHE_DIR":str(home)}
    if model:
        env["VRS_REMOTE_MODEL"]=model
    child=None
    try:
        child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"_execute",str(job)],
                               cwd=home, env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,start_new_session=True)
        atomic(job/"child.json",dict(pid=child.pid,birth=proc_birth(child.pid)))
        end=time.monotonic()+worker.limits["wall_seconds"]
        while child.poll() is None:
            if (job/"cancel").exists():
                status.update(state="cancelled",reason="Cancelled by client.")
                break
            if time.monotonic()>end or bytes_used(job)>worker.limits["max_storage"]:
                status.update(state="failed",reason="Worker runtime or disk limit exceeded.")
                break
            time.sleep(.25)
        if status["state"]=="running":
            result=job/"result"
            if child.returncode==0 and 0<result.stat().st_size<=worker.limits["max_transfer"]:
                status.update(state="succeeded",output=dict(bytes=result.stat().st_size,sha256=hash_file(result),
                    name="final.mp4" if read(job/"manifest.json")["operation"]=="render" else "transcript.json"))
            else:
                status.update(state="failed",reason="Processing failed; see the last reported stage.")
    except (OSError,ValueError):
        status.update(state="failed",reason="Worker could not complete the job.")
    finally:
        if child and child.poll() is None:
            try:
                os.killpg(child.pid,signal.SIGTERM)
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGKILL)
                child.wait()
            except ProcessLookupError:
                pass
        with worker.lock():
            if status["state"]=="succeeded" and (job/"cancel").exists():
                status.update(state="cancelled",reason="Cancelled by client.")
                status.pop("output",None)
            status["finished"]=time.time()
            worker.finish(job,status)


def execute(job, *, production_audio=False):
    manifest=read(job/"manifest.json")
    folder=job/"input"
    env=runtime_env()

    def progress(stage, percent):
        assert stage in PHASES
        atomic(job/"progress.json",dict(stage=stage,percent=percent))
        path=job/"events.json"
        events=read(path) if path.is_file() else []
        if not events or events[-1]["stage"]!=stage or percent//10>events[-1]["percent"]//10:
            events.append(dict(stage=stage,percent=percent,time=time.time()))
            atomic(path,events[-50:])

    def ffmpeg(*args):
        subprocess.run(["ffmpeg","-nostdin","-v","error","-xerror",*args],cwd=folder,check=True)

    if manifest["operation"]=="transcribe":
        from faster_whisper import WhisperModel
        progress("transcribe",5)
        ffmpeg("-f","wav","-i","voice.wav","-vn","-ac","1","-ar","16000",str(job/"scratch"/"decoded.wav"))
        model=WhisperModel(env["VRS_REMOTE_MODEL"], device="cpu",compute_type="int8",cpu_threads=2,
                           local_files_only=True)
        language=manifest["language"]
        segments,info=model.transcribe(str(job/"scratch"/"decoded.wav"),
                                       language=language if language in {"en","hi"} else None,word_timestamps=True)
        words=[dict(text=w.word.strip(),start=w.start,end=w.end,probability=w.probability)
               for segment in segments for w in segment.words if w.word.strip()]
        atomic(job/"result",dict(language=info.language,revision=manifest["revision"],words=words))
        progress("verify",100)
        return

    preset=manifest["preset"]
    width,height,fps,crf,quality=PRESETS[preset]
    raw=job/"scratch"/"capture.mp4"
    progress("capture",0)
    cmd=["node",str(ENGINE/"node_modules/hyperframes/dist/cli.js"),"render",str(folder),
         "--output",str(raw),"--fps",str(fps),"--crf",str(crf),"--quality",quality,
         "--workers","1","--strict","--no-best-effort","--refuse-outside-files"]
    with subprocess.Popen(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT) as child:
        for line in iter(lambda: child.stdout.readline(4096),b""):
            match=re.search(rb'@hf-progress .*?"pct"\s*:\s*(\d{1,3})',line)
            if match:
                progress("capture",min(100,int(match[1])))
        if child.wait():
            raise ValueError("Capture failed.")
    data=json.loads(output(["ffprobe","-v","error","-show_streams","-show_format","-of","json",str(raw)]))
    stream=next(w for w in data["streams"] if w["codec_type"]=="video")
    frames=int(stream["nb_frames"])
    if (stream["width"],stream["height"],stream["avg_frame_rate"])!=(width,height,f"{fps}/1") or not frames:
        raise ValueError("Capture does not match preset.")
    duration=f"{frames/fps:.9f}"
    progress("encode",5)
    args=["-i",str(raw)]
    voice=(folder/"voice.wav").is_file()
    if voice:
        voice_input = folder / "voice.wav"
        if production_audio:
            from production_render import mix_production_audio

            audio = folder / "assets" / "vrs-production-audio"
            cues = read(audio / "cues.json") if (audio / "cues.json").is_file() else []
            tracks = [(cue, audio / f"{index}.wav") for index, cue in enumerate(cues)]
            mixed = job / "scratch" / "mix.wav"
            mix_production_audio(voice_input, tracks, mixed, float(duration))
            voice_input = mixed
        args += ["-f", "wav", "-i", str(voice_input)]
    args+=["-map","0:v:0"]
    if (folder/"captions.ass").is_file():
        args+=["-vf","ass=captions.ass:fontsdir=assets/brand-profile","-c:v","libx264","-crf",str(crf),
               "-preset","fast","-pix_fmt","yuv420p"]
    else:
        args+=["-c:v","copy"]
    args+=["-map","1:a:0","-af","apad","-c:a","aac","-ar","48000"] if voice else ["-an"]
    ffmpeg(*args,"-t",duration,"-movflags","+faststart","-f","mp4",str(job/"result"))
    progress("verify",5)
    from production_render import verify
    verify(job/"result",frames/fps,fps,preset,voice)
    progress("verify",100)


def control(worker, args, stream, out):
    if not args:
        raise ValueError("Missing operation.")
    op,*params=args
    arities={"capabilities":0,"submit":2,"import":2,"status":1,"cancel":1,"fetch":1,"publish":1,"ack":1,"reap":0}
    if op not in arities or len(params)!=arities[op]:
        raise ValueError("Unsupported control command.")
    for param in params:
        valid_id(param)
    if op=="capabilities":
        return capability(worker.root,worker.limits)
    if op=="submit":
        return worker.submit(*params,stream,pipe=True)
    if op=="import":
        return worker.import_sftp(*params)
    with worker.lock():
        worker.reap()
        if op=="reap":
            return {"ok":True}
        ident=params[0]
        job=worker.path(ident)
        state=worker.status(ident)
        if op=="status":
            return state
        if op=="cancel" and state["state"]=="running":
            (job/"cancel").touch()
            return {**state,"cancelRequested":True}
        if op=="cancel":
            return state
        if op=="ack" and state["state"]=="cleaned":
            return state
        if state["state"]!="succeeded":
            raise ValueError("Job has no downloadable result.")
        if op=="ack":
            worker.clean(job,state)
            return worker.status(ident)
        if op=="fetch":
            with (job/"result").open("rb") as src:
                copy_bounded(src,out,worker.limits["max_transfer"])
            return None
        if op=="publish" and worker.exchange:
            target=worker.exchange/"results"/(ident+".result")
            record=state["output"]
            if not (target.is_file() and not target.is_symlink()
                    and target.stat().st_size==record["bytes"] and hash_file(target)==record["sha256"]):
                with tempfile.NamedTemporaryFile(dir=target.parent,prefix=".publish-",delete=False) as dest:
                    temporary=Path(dest.name)
                    try:
                        with (job/"result").open("rb") as src:
                            copy_bounded(src,dest,record["bytes"])
                        dest.flush()
                        if temporary.stat().st_size!=record["bytes"] or hash_file(temporary)!=record["sha256"]:
                            raise ValueError("Stored output checksum mismatch.")
                        temporary.chmod(0o640)
                        os.replace(temporary,target)
                    finally:
                        temporary.unlink(missing_ok=True)
            return state
        raise ValueError("SFTP output exchange is not configured.")


def self_test(worker):
    cap = capability(worker.root, worker.limits)
    if not cap["operations"]["render"]:
        raise ValueError("Worker render prerequisites are incomplete.")
    env = runtime_env()
    script = (
        "import {createRequire} from 'node:module';"
        "const r = createRequire(new URL('./package.json', import.meta.url));"
        "const {launch} = r('puppeteer-core');"
        "const b = await launch({executablePath: process.env.VRS_REMOTE_BROWSER, headless: true,"
        "args: ['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});"
        "try{const p=await b.newPage();"
        "const ok=await p.evaluate(()=>!!document.createElement('canvas').getContext('webgl2'));"
        "if(!ok) throw new Error('WebGL2 unavailable');"
        "} finally {await b.close();}"
    )
    with tempfile.TemporaryDirectory(prefix="selftest-", dir=worker.root) as tmp:
        scratch = Path(tmp)
        env.update(HOME=str(scratch), TMPDIR=str(scratch), XDG_CACHE_HOME=str(scratch))
        subprocess.run(["node", "--input-type=module", "-e", script], cwd=ENGINE, env=env,
                       stdout=subprocess.DEVNULL, check=True, timeout=45)
    cap["webgl"] = "verified (sandboxed Chromium WebGL 2)"
    return cap


def main():
    os.umask(0o077)
    if len(sys.argv)==3 and sys.argv[1] in {"_run","_execute"}:
        (run_job if sys.argv[1]=="_run" else execute)(Path(sys.argv[2]))
        return
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state",type=Path,required=True)
    parser.add_argument("--limits",type=Path)
    parser.add_argument("--exchange",type=Path)
    parser.add_argument("--forced",action="store_true")
    parser.add_argument("--self-test",action="store_true")
    parser.add_argument("command",nargs="*")
    cfg=parser.parse_args()
    try:
        worker=Worker(cfg.state,read(cfg.limits) if cfg.limits else None,cfg.exchange)
        if cfg.self_test:
            if cfg.forced:
                raise ValueError("Self-test cannot run under a forced SSH command.")
            print(encode(self_test(worker)).decode())
            return
        args=os.environ.get("SSH_ORIGINAL_COMMAND","").split() if cfg.forced else cfg.command
        result=control(worker,args,sys.stdin.buffer,sys.stdout.buffer)
        if result is not None:
            print(encode(result).decode())
    except (ValueError,OSError,KeyError,BadZipFile,RuntimeError,TypeError,subprocess.SubprocessError) as error:
        parser.exit(1,f"Worker rejected request: {error}\n")


if __name__=="__main__":
    main()
