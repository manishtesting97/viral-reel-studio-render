"""Production-plan validation, staging and narration alignment."""
import hashlib
import json
import math
import os
import resource
import time
from studio_paths import MODELS
from pathlib import Path
import re
import tempfile

from alignment import DEVANAGARI, MIN_ANCHOR_CONFIDENCE, QUALITY_FLOOR, match_script, normalized, valid_span
from preview_phase2 import BRAND_DIR, CACHE, ENGINE, ROOT, STYLE_LIB, probe_duration, run
from voiceover_director import (allowed, CATALOG, INTENTS, clean_narration, director_view, possible_spoken_tags,
                                preferences, validate_expressive, validate_scene)

SCHEMA = ENGINE / "production.schema.json"
CONFIG = tuple("id topic audience niche language duration brand style".split())
PROJECTS = ROOT / "projects"
TOKEN = re.compile(r"[^\W_][\w\u0900-\u097f'’]*", re.UNICODE)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        file.write(text.encode("utf-8"))
    Path(file.name).replace(path)


def save(path, data):
    write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def digest(*inputs):
    key = hashlib.sha256()
    for item in inputs:
        if isinstance(item, Path):
            with item.open("rb") as file:
                data = hashlib.file_digest(file, "sha256").hexdigest()
        else:
            data = json.dumps(item, sort_keys=True, ensure_ascii=False)
        key.update(data.encode("utf-8") + b"\0")
    return key.hexdigest()[:20]


def timing_key(voice, plan):
    # Timing is tied to the recorded voice and clean scene plan.
    base = {key: value for key, value in plan.items() if key != "badge"}
    base["scenes"] = [{key: value for key, value in scene.items() if key != "voice_direction"}
                      for scene in plan["scenes"]]
    return digest(voice, base)


def current_alignment_report(voice, plan, key=None):
    path = voice.with_name("alignment-report.json")
    if not voice.is_file() or not path.is_file():
        return {}
    report = load(path)
    if "timingKey" in report:
        return report if report["timingKey"] == (key or timing_key(voice, plan)) else {}
    if report.get("recognitionCache") and report.get("model"):
        filename = f".whisper-{digest(voice, plan['language'], report['model'])}.json"
        return report if report["recognitionCache"] == filename else {}
    return report  # Legacy reports have no input fingerprint.


def timing_is_current(timeline, voice, plan):
    if not timeline.get("key") or not voice.is_file():
        return False
    key = timing_key(voice, plan)
    return (timeline["key"] in {key, digest(voice, plan)}
            and current_alignment_report(voice, plan, key).get("status") not in {"failed", "running"})


def validate(value, rule, path="$"):
    """Evaluate the JSON Schema keywords used in the versioned contract."""
    kind = rule.get("type")
    valid = {
        "object": type(value) is dict,
        "array": type(value) is list,
        "string": type(value) is str,
        "integer": type(value) is int,
        "number": type(value) in (int, float) and abs(value) <= 1e100,
    }
    if (kind and not valid[kind]) or ("const" in rule and value != rule["const"]):
        raise ValueError(f"{path}: expected {rule.get('const', kind)}")
    if "enum" in rule and value not in rule["enum"]:
        raise ValueError(f"{path}: expected one of {rule['enum']}")
    if kind == "object":
        req = set(rule.get("required", ()))
        if not req.issubset(value) or not set(value).issubset(rule["properties"]):
            raise ValueError(f"{path}: missing {sorted(req - value.keys())}, "
                             f"unknown {sorted(value.keys() - set(rule['properties']))}")
        for key, item in value.items():
            validate(item, rule["properties"][key], f"{path}.{key}")
    elif kind == "array":
        if not rule["minItems"] <= len(value) <= rule["maxItems"]:
            raise ValueError(f"{path}: invalid item count")
        for index, item in enumerate(value):
            validate(item, rule["items"], f"{path}[{index}]")
    elif kind == "string":
        if not value.strip() or not rule["minLength"] <= len(value) <= rule["maxLength"]:
            raise ValueError(f"{path}: invalid text length")
        if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
            raise ValueError(f"{path}: invalid identifier or relative asset path")
    elif kind == "number" and not rule["minimum"] <= value <= rule["maximum"]:
        raise ValueError(f"{path}: out of range")


def profiles(config, project=None):
    style_id = config["style"]
    if project is not None:
        vc_path = Path(project) / "visual-config.json"
        if vc_path.is_file():
            try:
                override = load(vc_path).get("style")
                if (isinstance(override, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,47}", override)
                        and (STYLE_LIB / f"{override}.json").is_file()):
                    style_id = override
            except Exception:
                pass
    return load(BRAND_DIR / f"{config['brand']}.json"), load(STYLE_LIB / f"{style_id}.json")


def init_project(project, config):
    schema = load(SCHEMA)
    for key in CONFIG:
        validate(config[key], schema["properties"][key], key)
    if project.exists():
        raise ValueError("Project already exists. Resume using its saved files.")
    profiles(config)
    save(project / "brief.json", config)
    write_prompt(project, config)


def write_prompt(project, config, badge=None):
    write(project / "production-prompt.txt", production_prompt(project, config, badge))


def production_prompt(project, config, badge=None):
    from sponsorship import prompt_context

    brand, style = profiles(config)
    existing_plan = project / "plan.json"
    sponsor_context = prompt_context(project, load(existing_plan) if existing_plan.is_file() else None)
    prefs = preferences(director_state(project).get("preferences", {}))
    return ("Create an original vertical Instagram Reel production plan. Return only one JSON object "
          "conforming to the schema below. Copy the brief values exactly. Use contiguous estimated "
          "scene times from 0 through duration, and scene-relative animation/audio cue times. "
          "Put only words to be spoken in narration, spell out numbers, and prefer clear sentences. "
          "For each scene, decide whether a change of delivery helps the listener. Tailor delivery to that scene's purpose, actual "
          "narration, niche and audience. For Hinglish, favor natural conversational Indian delivery and "
          "keep the approved code-switching. Use an optional scene note plus sparse cues with one intent "
          "from voiceDirection.intents (or a bare tag from allowedTags) and a zero-based beforeWord "
          "index into the whitespace-separated narration (word count means after the last word). "
          "A cue can start a scene, a sentence or a meaningful clause within a sentence. Use cues only when "
          "useful: highlight a hook, reveal, contrast or caution with a suitable emotion or delivery change. "
          "Do not return pause-only directions for every scene when the narration supports other expressions. "
          "Leave neutral scenes untagged. Add a vocal reaction only when the spoken beat warrants it. "
          "Avoid repetitive emotions, excessive pauses, overacting and irrelevant effects. "
          "Keep the spoken wording identical and never put tags in narration. "
          "Specify original scene-specific visuals; use the style profile as art direction and "
          "the brand profile as identity. Asset entries are local relative filenames, never URLs. "
          "Optionally populate each scene's `storyboard` with purpose, composition, semantic entities, "
          "UI elements, narration word/phrase anchors, and motion events (reveal, type, click, drag, "
          "connect, count, highlight, transform, confirm, reject, restore, compare, exit) timed to spoken words. "
          "Do not fabricate product screenshots, medical claims, metrics or unsupported facts.\n\n"
          + json.dumps({"brief": config, "brand": brand, "referenceStyle": style,
                        **({"badge": badge} if badge else {}),
                        **({"sponsorship": sponsor_context} if sponsor_context else {}),
                        "voiceDirection": {"style": prefs["style"], "intensity": prefs["intensity"],
                                           "model": prefs["model"],
                                           "intents": INTENTS,
                                           "allowedTags": [tag for tags in CATALOG.values()
                                                           for tag in tags if allowed(tag, prefs)]},
                        "schema": load(SCHEMA)}, ensure_ascii=False, indent=2) + "\n")


def director_state(project):
    path = project / "voiceover-director.json"
    return load(path) if path.is_file() else {}


def save_director(project, plan, selected, edited=None):
    prefs = preferences(selected)
    if set(prefs["sceneOverrides"]) - {scene["id"] for scene in plan["scenes"]}:
        raise ValueError("A voice override refers to a scene outside the current plan.")
    state = {"preferences": prefs}
    if edited is not None:
        state["edited"] = validate_expressive(edited, clean_narration(plan), prefs)
    view = director_view(plan, state)
    save(project / "voiceover-director.json", state)
    write(project / "voiceover-expressive.txt", view["expressive"])


def import_plan(project, plan):
    validate(plan, load(SCHEMA))
    brief = load(project / "brief.json")
    if any(plan[key] != brief[key] for key in CONFIG):
        raise ValueError("Plan must match the saved brief.")
    expected = 0
    scene_ids, audio_ids = {"production", "stage"}, set()
    for scene in plan["scenes"]:
        validate_scene(scene)
        sid = scene["id"]
        if sid in scene_ids or not math.isclose(scene["start"], expected, abs_tol=1e-6):
            raise ValueError(f"{sid}: duplicate ID or discontinuous scene timeline.")
        scene_ids.add(sid)
        expected = scene["end"]
        length = expected - scene["start"]
        if length <= 0 or not TOKEN.search(scene["narration"]):
            raise ValueError(f"{sid}: expected positive duration and spoken words.")
        motion_ids = set()
        for motion in scene["animation"]:
            if motion["id"] in motion_ids or not 0 <= motion["start"] < motion["end"] <= length + 1e-6:
                raise ValueError(f"{sid}: invalid animation interval or duplicate ID.")
            motion_ids.add(motion["id"])
        for cue in scene["audio_cues"]:
            if cue["id"] in audio_ids or not 0 <= cue["at"] < length:
                raise ValueError(f"{sid}: invalid audio cue position or duplicate ID.")
            audio_ids.add(cue["id"])
    if not math.isclose(expected, plan["duration"], abs_tol=1e-6):
        raise ValueError("Final estimated scene must end at the brief duration.")
    extract(project, plan)
    save(project / "plan.json", plan)


def extract(project, plan):
    view = director_view(plan, director_state(project))
    write(project / "voiceover.txt", clean_narration(plan))
    write(project / "voiceover-expressive.txt", view["expressive"])
    save(project / "scenes.json", plan["scenes"])
    save(project / "instagram.json", plan["instagram"])


def local_audio(source, dest):
    if source.suffix.lower() not in {".wav", ".mp3"} or not source.is_file():
        raise ValueError("Select an existing local .wav or .mp3 file.")
    run("ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
        "-i", source.resolve(), "-map", "0:a:0", "-c:a", "pcm_s16le", "-ar", "48000", dest)


def draft_voice(project, plan, dest):
    if plan["language"] != "en":
        raise ValueError("Offline Flite draft voice supports English. Supply MP3/WAV for other languages.")
    extract(project, plan)
    run("ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
        "flite=textfile=voiceover.txt:voice=slt", "-ar", "48000", dest, cwd=project)


def alignment_stage(stage, pct):
    print("@vrs-align " + json.dumps({"stage": stage, "pct": pct}), flush=True)


def retry_recognition(observed, retry, cache_name, duration):
    """Splice one cached local ASR retry using only whole measured word spans."""
    if retry["recognitionCache"] != cache_name:
        raise ValueError("The segment retry belongs to another recognition cache.")
    start, end = retry["replace"]
    clip_start, clip_end = retry["clip"]
    if not 0 <= clip_start <= start < end <= clip_end <= duration:
        raise ValueError("Segment retry lies outside the current audio.")
    if any(not valid_span([word], duration) for word in retry["words"]):
        raise ValueError("Segment retry contains an invalid measured span.")
    before = [word for word in observed if word["start"] < start]
    after = [word for word in observed if word["start"] >= start and word["end"] > end]
    left = max(start, before[-1]["end"] if before else start)
    right = min(end, after[0]["start"] if after else end)
    middle = [word for word in retry["words"] if left <= word["start"] < word["end"] <= right]
    if any(a["end"] > b["start"] for a, b in zip(middle, middle[1:])):
        raise ValueError("Segment retry overlaps its own word spans.")
    return before + middle + after if middle else observed


def witness_windows(report, duration):
    """Limit independent ASR to three short recognition gaps and 12 seconds total."""
    windows = set()
    for row in report["unmatched"]:
        span = row.get("reviewWindow") or row.get("candidate")
        if span and 0 < span["end"] - span["start"] <= 3:
            windows.add((max(0, span["start"] - .30), min(duration, span["end"] + .30)))
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] and end - merged[-1][0] <= 4:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        elif not merged or start > merged[-1][1]:
            merged.append((start, end))
    return merged[:3]


def align(project, plan, model_name=None, *, remote_words=None):
    for scene in plan["scenes"]:
        validate_scene(scene)
    voice = project / "voice.wav"
    model_name = model_name or ("tiny.en" if plan["language"] == "en" else
                                "tiny" if (MODELS / "models--Systran--faster-whisper-tiny/refs/main").is_file()
                                else "small")
    key = digest(voice, plan["language"], model_name)
    transcript = project / f".whisper-{key}.json"
    report_path = project / "alignment-report.json"
    identity = {"timingKey": timing_key(voice, plan), "model": model_name,
                "recognitionCache": transcript.name}
    save(report_path, {**identity, "status": "running"})
    if remote_words is not None:
        save(transcript, remote_words)
    model = None

    def recognize(language, *, samples=None, offset=0, override_model=None, **options):
        nonlocal model
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        from faster_whisper import WhisperModel

        if override_model and override_model != model_name:
            # Free primary model first on memory-constrained CPUs before loading fast witness model.
            model = None
            target = WhisperModel(override_model, device="cpu", compute_type="int8", cpu_threads=2,
                                  download_root=str(MODELS), local_files_only=True)
        else:
            if model is None:
                model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=2,
                                     download_root=str(MODELS), local_files_only=True)
            target = model
        segments, info = target.transcribe(str(voice) if samples is None else samples, language=language,
                                           word_timestamps=True, **options)
        return ([{"text": word.word.strip(), "start": word.start + offset, "end": word.end + offset,
                  "probability": word.probability} for segment in segments
                 for word in segment.words if normalized(word.word)],
                {"configuredLanguage": language or "auto", "language": info.language,
                 "languageProbability": info.language_probability})

    t_align_start = time.perf_counter()
    primary_transcribe_s = 0.0
    witness_transcribe_s = 0.0
    info_path = project / f".whisper-{key}-recognition.json"
    if not transcript.is_file():
        alignment_stage("Transcribing local audio", 10)
        t_p0 = time.perf_counter()
        recognition, metadata = recognize({"en": "en", "hi": "hi"}.get(plan["language"]))
        primary_transcribe_s = round(time.perf_counter() - t_p0, 3)
        save(transcript, recognition)
        save(info_path, metadata)
    if info_path.is_file():
        identity["recognition"] = load(info_path)
    alignment_stage("Matching cached recognition to script", 65)
    observed = load(transcript)
    duration = probe_duration(voice)
    words, report = match_script(plan["scenes"], observed, duration, plan["language"])
    retry_path = project / f".whisper-{key}-retry.json"
    if retry_path.is_file():
        retry = load(retry_path)
        candidate = retry_recognition(observed, retry, transcript.name, duration)
        candidate_words, candidate_report = match_script(plan["scenes"], candidate, duration, plan["language"])
        if (candidate_report["reliableMatches"] > report["reliableMatches"]
                and candidate_report["reliableCoverage"] > report["reliableCoverage"]
                and candidate_report["coverage"] >= report["coverage"]):
            observed, words, report = candidate, candidate_words, candidate_report
            report["segmentRetry"] = {"cache": retry_path.name, "clip": retry["clip"], "replace": retry["replace"],
                                      "rawCache": transcript.name, "method": retry.get("method")}
    has_tiny_en = (MODELS / "models--Systran--faster-whisper-tiny.en/refs/main").is_file()
    if (plan["language"] == "hinglish" and model_name != "tiny.en" and remote_words is None
            and duration <= 180 and (report["reliableCoverage"] < QUALITY_FLOOR or report["unmatched"])
            and any(DEVANAGARI.search(w["text"]) for w in observed)):
        use_tiny_en = has_tiny_en
        windows = [(0, duration)] if (use_tiny_en or duration <= 12) else witness_windows(report, duration)
        suffix = ("tiny-en-witness-v2" if use_tiny_en
                  else "english-witness-v1" if duration <= 12
                  else f"english-windows-v1-{digest(windows)}")
        alternate_path = project / f".whisper-{key}-{suffix}.json"
        try:
            if not alternate_path.is_file():
                alignment_stage("Checking measured code-switch gaps with independent ASR", 72)
                t_w0 = time.perf_counter()
                options = {"beam_size": 3, "condition_on_previous_text": False}
                if use_tiny_en:
                    alternate, metadata = recognize("en", override_model="tiny.en", **options)
                    metadata["witnessModel"] = "tiny.en"
                elif duration <= 12:
                    alternate, metadata = recognize("en", **options)
                else:
                    alternate, metadata = [], {"configuredLanguage": "en"}
                    if windows:
                        from faster_whisper.audio import decode_audio
                        sample_rate = 16000
                        audio = decode_audio(str(voice), sampling_rate=sample_rate)
                        for start, end in windows:
                            left, right = round(start * sample_rate), round(end * sample_rate)
                            if right > left:
                                measured, _ = recognize("en", samples=audio[left:right],
                                                        offset=left / sample_rate, **options)
                                alternate.extend(w for w in measured if left / sample_rate <= w["start"]
                                                 < w["end"] <= right / sample_rate)
                witness_transcribe_s = round(time.perf_counter() - t_w0, 3)
                save(alternate_path, {"words": alternate, "recognition": metadata,
                                      "windows": windows, "prompted": False})
            evidence = load(alternate_path)
            revised, checked = match_script(plan["scenes"], observed, duration, plan["language"],
                                            alternate=evidence["words"] if evidence.get("prompted") is False else [])
        except (ImportError, OSError, RuntimeError, ValueError, KeyError):
            report["independentRecognition"] = {"status": "unavailable"}
        else:
            if (checked["corroboratedMatches"] > report.get("corroboratedMatches", 0)
                    or checked["reliableCoverage"] > report["reliableCoverage"]
                    or checked["coverage"] > report["coverage"]):
                checked.update({k: report[k] for k in ("segmentRetry",) if k in report})
                words, report = revised, checked
            report["independentRecognition"] = {"cache": alternate_path.name,
                                                **evidence.get("recognition", {}),
                                                "windows": evidence.get("windows", windows),
                                                "prompted": evidence.get("prompted")}
    expressive = director_view(plan, director_state(project))["expressive"]
    report["spokenDirections"] = possible_spoken_tags(
        expressive, observed, words, plan["language"] in {"hi", "hinglish"}, duration)
    total_align_s = round(time.perf_counter() - t_align_start, 3)
    peak_rss_mib = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    report["performance"] = {
        "primaryTranscribeSeconds": primary_transcribe_s,
        "witnessTranscribeSeconds": witness_transcribe_s,
        "alignmentTotalSeconds": total_align_s,
        "peakRssMiB": peak_rss_mib,
        "usedPrimaryCache": primary_transcribe_s == 0.0,
    }
    if len(report["unmatched"]) > 12 or report["reliableCoverage"] < QUALITY_FLOOR:
        report["diagnosticGuidance"] = (
            f"{len(report['unmatched'])} words still need measured timing ({report['reliableCoverage']:.1%} strong evidence vs "
            f"{QUALITY_FLOOR:.0%} gate). Check whether voiceover pronunciation matches the script phrasing (especially mixed "
            "English/Hindi technical words) or review each flagged interval below before retrying alignment."
        )
    elif report["unmatched"]:
        report["diagnosticGuidance"] = (
            f"Gate passed ({report['reliableCoverage']:.1%}). Review the {len(report['unmatched'])} remaining unmatched words "
            "below using Play isolated candidate to accept exact spoken spans before Premium export."
        )
    report.update(identity, voiceDuration=duration)
    anchors = [next((w for w in words if w["scene"] == scene["id"]), None) for scene in plan["scenes"]]
    report["sceneAnchors"] = [
        {"scene": scene["id"], "opening": next(token for token in scene["narration"].split() if normalized(token)),
         **({"expected": word["text"], "heard": word["heard"], "start": word["start"], "end": word["end"],
             "match": word["match"], "confidence": word["confidence"]} if word else {})}
        for scene, word in zip(plan["scenes"], anchors)
    ]
    if report["reliableCoverage"] < QUALITY_FLOOR:
        failure = (f"Strong script coverage {report['reliableCoverage']:.1%} is below {QUALITY_FLOOR:.0%}. "
                   "Review the expected and recognized words, then retry using the same audio.")
    elif None in anchors or any(not any(w["scene"] == scene["id"] and w["match"] != "approximate"
                                       and w["probability"] >= MIN_ANCHOR_CONFIDENCE for w in words)
                                for scene in plan["scenes"]):
        failure = "At least one scene lacks a reliable measured speech anchor. Review the script and recognition."
    elif any(row["expected"].split()[0] != row["opening"] for row in report["sceneAnchors"]):
        failure = "No accepted timing for a scene opening. Review its source audio before continuing."
    else:
        failure = None
    if failure:
        save(report_path, {**report, "status":"failed", "failureReason":failure})
        raise ValueError(failure)
    starts = [word["start"] for word in anchors]
    alignment_stage("Saving measured scene and caption timing", 90)
    starts[0] = 0
    total = duration + plan["outro"]
    scenes = []
    for scene, start, end in zip(plan["scenes"], starts, starts[1:] + [total]):
        # An explicit outro holds the final scene without stretching its motion.
        active_end = min(end, duration)
        scale = (active_end - start) / (scene["end"] - scene["start"])
        scenes.append({"id": scene["id"], "start": start, "end": end,
                       "animation": [{**a, "start": a["start"] * scale, "end": a["end"] * scale}
                                     for a in scene["animation"]],
                       "audio_cues": [{**c, "at": start + c["at"] * scale}
                                      for c in scene["audio_cues"]]})
    timeline = {"version": 1, "key": identity["timingKey"], "voiceDuration": duration,
                "duration": total, "scenes": scenes, "words": words}
    save(project / "timeline.json", timeline)
    save(report_path, {**report, "status":"passed"})
    return timeline
