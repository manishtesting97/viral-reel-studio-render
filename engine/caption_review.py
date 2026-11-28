"""Compose a current caption track from measured alignment and explicit local review."""
from collections import defaultdict
from copy import deepcopy
import math

from alignment import MIN_ANCHOR_CONFIDENCE, QUALITY_FLOOR, REVIEW_CONFIDENCE, lexical, near, normalized, strong_pairs
from production_plan import current_alignment_report, digest, load, measured_timeline, save, timing_is_current


def script_entries(plan):
    return [(scene["id"], token) for scene in plan["scenes"]
            for token in scene["narration"].split() if normalized(token)]


def position_words(timeline, report, script):
    """Legacy timelines predate script indices; reject ambiguous repeated-word placements."""
    words = deepcopy(timeline["words"])
    original = {(w["scene"], w["start"], w["end"]): w for w in report.get("matches", [])}

    def positions(reverse):
        cursor = len(script) if reverse else 0
        for word in (reversed(words) if reverse else words):
            base = original.get((word["scene"], word["start"], word["end"]), word)
            tokens = [(word["scene"], token) for token in base["text"].split() if normalized(token)]
            count = len(tokens)
            fixed = word.get("scriptIndex")
            choices = (fixed,) if fixed is not None else (
                range(cursor-count, -1, -1) if reverse else range(cursor, len(script)-count+1))
            i = next((i for i in choices if script[i:i+count] == tokens
                      and (i+count <= cursor if reverse else i >= cursor)), None)
            if i is None:
                raise ValueError("Caption positions do not match the approved script. Refresh alignment first.")
            cursor = i if reverse else i+count
            yield i

    forward, backward = list(positions(False)), list(positions(True))[::-1]
    if forward != backward:
        raise ValueError("The cached alignment has ambiguous repeated words. Refresh alignment before editing.")
    return {i: {**word, "scriptIndex": i, "scriptWords": word.get("scriptWords", 1)}
            for i, word in zip(forward, words)}


def asr_windows(project, report):
    """Use independent cached passes bound to the exact audio and ASR revision."""
    name = report.get("recognitionCache", "")
    key = report.get("timingKey")
    if not name.startswith(".whisper-") or "/" in name or "\\" in name:
        return []
    path = project / "caption-asr-windows.json"
    if not path.is_file() or not (project / name).is_file():
        return []
    data = load(path)
    if data.get("timingKey") != key or data.get("recognitionCache") != name:
        return []
    windows = [load(project / name)]
    retry = project / (name.removesuffix(".json") + "-retry.json")
    if retry.is_file():
        payload = load(retry)
        if payload.get("recognitionCache") == name:
            windows.append(payload["words"])
    windows.extend(window["words"] for window in data["windows"])
    return list({digest(window): window for window in windows}.values())


def recover(script, words, missing, windows, duration, multilingual):
    """Only recover a bracketed word when separate passes agree on its full measured span."""
    recovered = {}
    anchors = sorted(words)
    for left, right in zip(anchors, anchors[1:]):
        previous, following = words[left], words[right]
        first = left + previous["scriptWords"]
        if first >= right or right-first > 6 or previous["scene"] != following["scene"]:
            continue
        gap = script[first:right]
        evidence = defaultdict(list)
        for source, observed in enumerate(windows):
            samples = [w for w in observed if previous["end"] <= w["start"] < w["end"] <= following["start"]
                       and w["probability"] >= REVIEW_CONFIDENCE]
            for a,b,c,d,kind in strong_pairs(gap, samples, duration, multilingual):
                if b-a != 1:
                    continue
                selected = samples[c:d]
                if d-c == 1 and sum(bool(lexical(gap[a][1], w["text"], multilingual)) for w in samples) != 1:
                    continue
                evidence[first+a].append((source, selected, True))
            for a, (_, expected) in enumerate(gap):
                # One close spelling can corroborate an exact/normalized pass.
                # It cannot establish a timed word on its own.
                if len(normalized(expected)) < 4 or any(row[0] == source for row in evidence[first+a]):
                    continue
                variants = [w for w in samples if near(expected, w["text"], multilingual)]
                if len(variants) == 1:
                    evidence[first+a].append((source, variants, False))
        for i, rows in sorted(evidence.items()):
            if i not in missing:
                continue
            for source, tokens, strict in rows:
                if not strict:
                    continue
                start, end = tokens[0]["start"], tokens[-1]["end"]
                if not any(other != source and abs(seq[0]["start"]-start) <= .08
                           and abs(seq[-1]["end"]-end) <= .08
                           and min(end,seq[-1]["end"])-max(start,seq[0]["start"]) >= .6*(end-start)
                           for other, seq, _ in rows):
                    continue
                if recovered and max(w["end"] for w in recovered.values()) > start:
                    continue
                recovered[i] = dict(scene=script[i][0], text=script[i][1], start=start,end=end,
                                    scriptIndex=i, scriptWords=1, heard=" ".join(w["text"] for w in tokens),
                                    probability=min(w["probability"] for w in tokens),
                                    confidence="measured",match="normalized", timingSource="cached ASR consensus")
                break
    return recovered


def caption_state(project, plan, timeline, report, mutation=None):
    """Read or update the correction sidecar; leave the base alignment and scene cuts intact."""
    failed = report.get("status") == "failed"
    if not report.get("scriptWords") or report.get("status") not in {"passed", "failed"}:
        return timeline, report
    script = script_entries(plan)
    words = position_words(timeline, report, script)
    gaps = set(range(len(script))) - {j for i,w in words.items() for j in range(i,i+w["scriptWords"])}
    missing = {}
    gaps_by_word = defaultdict(list)
    for i in sorted(gaps, reverse=True):
        gaps_by_word[script[i]].append(i)
    for item in report.get("unmatched", []):
        expected = (item["scene"], item["text"])
        i = item.get("scriptIndex")
        if i is None:
            choices = gaps_by_word[expected]
            while choices and choices[-1] not in gaps:
                choices.pop()
            i = choices.pop() if choices else None
        if i in gaps and script[i] == expected:
            missing[i] = item
            gaps.remove(i)
    for i in sorted(gaps):
        missing[i] = {"scene":script[i][0], "text":script[i][1], "match":"unmatched",
                      "reason":"No accepted measured interval for this script word."}
    flags = report.get("reviewWords")
    if flags is None:
        flags = [{**word, "reason":"Low recognition confidence."} for word in words.values()
                 if word.get("probability", 1) < REVIEW_CONFIDENCE]
    flagged = {(item["scene"],item["start"],item["end"]):item for item in flags}
    review = {i: flagged[key] for i,w in words.items()
              if (key := (w["scene"],w["start"],w["end"])) in flagged}
    opening_reviews = {i for i,w in words.items() if any(
        a.get("match") == "approximate" and (a["scene"],a.get("start")) == (w["scene"],w["start"])
        for a in report.get("sceneAnchors", []))}
    required = set(opening_reviews)
    if failed:
        required.update(i for i,w in words.items()
                        if w.get("match") == "approximate" or w.get("probability", 1) < REVIEW_CONFIDENCE)
        scene_starts = {i for i,(scene,_) in enumerate(script) if i == 0 or script[i-1][0] != scene}
        opening_reviews.update(i for i in scene_starts if i in required or i in missing)
        for i in required:
            review.setdefault(i, {**words[i], "reason":"Listen and verify this measured interval."})
    for i in opening_reviews & words.keys():
        review.setdefault(i, {**words[i], "reason":"Listen for the first spoken word and confirm its caption onset."})
    windows = asr_windows(project, report)
    automatic = recover(script, words, missing, windows, timeline["voiceDuration"], plan["language"] != "en")
    words.update(automatic)
    # Both immutable base inputs and the current edits identify the submitted form.
    base = (digest(timeline, {key:report.get(key) for key in
                              ("timingKey", "matches", "reviewWords", "unmatched", "sceneAnchors")})
            if failed else digest(timeline, report))
    path = project / "caption-corrections.json"
    state = load(path) if path.is_file() else {}
    targets = {**missing, **review}
    scenes = {s["id"]: s for s in timeline["scenes"]}
    if state.get("base") == base:
        edits = dict(state.get("edits", {}))
    elif state.get("timingKey") in {None, report.get("timingKey")} and isinstance(state.get("edits"), dict):
        # Preserve still-valid manual edits matching script identity across recovery retries/restarts.
        edits = {}
        for k, v in state["edits"].items():
            try:
                i = int(k)
            except (TypeError, ValueError):
                continue
            if i not in targets or not isinstance(v, dict):
                continue
            if (v.get("scene") is not None and v["scene"] != script[i][0]) or (
                    v.get("text") is not None and v["text"] != script[i][1]):
                continue
            s_val, e_val = v.get("start"), v.get("end")
            scene = scenes.get(targets[i]["scene"], {"start": 0, "end": timeline["voiceDuration"]})
            if (all(type(n) in (int, float) and math.isfinite(n) for n in (s_val, e_val))
                    and scene["start"] <= s_val < e_val <= min(scene["end"], timeline["voiceDuration"])):
                edits[str(i)] = v
    else:
        edits = {}
    skips = {
        str(i): {"scene": script[i][0], "text": script[i][1]}
        for i in missing.keys() - words.keys() - opening_reviews
        if state.get("timingKey") == report.get("timingKey")
        and state.get("skips", {}).get(str(i)) == {"scene": script[i][0], "text": script[i][1]}
        and str(i) not in edits
    }
    revision = digest(base, edits, windows, skips)
    original_words = words.copy()

    def apply(i, start, end):
        target = targets[i]
        previous = max((w["end"] for j,w in words.items() if j<i), default=0)
        following = min((w["start"] for j,w in words.items() if j>i), default=timeline["voiceDuration"])
        scene = scenes.get(target["scene"], {"start":0, "end":timeline["voiceDuration"]})
        if not (max(previous,scene["start"]) <= start < end <= min(following,scene["end"],timeline["voiceDuration"])):
            raise ValueError("Timing must stay inside the scene and between neighbouring captions.")
        w = words.get(i, {})
        words[i] = {**w, "scene": target["scene"], "text": w.get("text",target["text"]),
                    "heard": target.get("heard", ""), "start":start,"end":end, "scriptIndex":i,
                    "scriptWords":w.get("scriptWords",1),"match":"manual","confidence":"manual",
                    "timingSource":"manually verified"}

    if mutation is not None:
        if mutation.get("revision") != revision:
            raise ValueError("Caption review changed. Reload this step before saving.")
        action = mutation.get("action")
        if action in {"skip", "skip_remaining"}:
            if mutation.get("accepted") is not True:
                raise ValueError("Confirm that the selected caption words should be skipped.")
            available = missing.keys() - words.keys() - opening_reviews - {int(i) for i in edits}
            available -= {int(i) for i in skips}
            if action == "skip":
                index = mutation.get("index")
                if type(index) is not int or index not in available:
                    raise ValueError("Only unresolved missing words can be skipped. Scene openings require verified timing.")
                available = {index}
            if not available:
                raise ValueError("There are no missing caption words eligible to skip.")
            for i in available:
                skips[str(i)] = {"scene": script[i][0], "text": script[i][1]}
        elif action == "restore_skip":
            index = mutation.get("index")
            if type(index) is not int or skips.pop(str(index), None) is None:
                raise ValueError("There is no skipped caption word to restore.")
        elif action == "bulk_accept":
            if mutation.get("accepted") is not True:
                raise ValueError("Listen and confirm before bulk-accepting measured timing suggestions.")
            items = mutation.get("items")
            if not isinstance(items, list) or not items or len(items) > len(targets):
                raise ValueError("Select at least one valid measured timing suggestion.")
            seen_bulk = set()
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("Invalid bulk acceptance entry.")
                idx = item.get("index")
                if type(idx) is not int or idx not in targets or idx in seen_bulk:
                    raise ValueError("Unknown or duplicate bulk caption review entry.")
                seen_bulk.add(idx)
                start, end = item.get("start"), item.get("end")
                if any(type(n) not in (int, float) or not math.isfinite(n) for n in (start, end)) or not start < end:
                    raise ValueError("Measured timing suggestions require valid start and end times.")
                if idx in review:
                    cand = review[idx]
                elif isinstance(missing.get(idx, {}).get("candidate"), dict):
                    cand = missing[idx]["candidate"]
                else:
                    raise ValueError("Only entries with valid measured timing suggestions can be bulk-accepted.")
                if abs(cand["start"] - start) > 1e-4 or abs(cand["end"] - end) > 1e-4:
                    raise ValueError("Submitted interval does not match the measured suggestion.")
                edits[str(idx)] = {"start": start, "end": end, "scene": script[idx][0], "text": script[idx][1]}
        else:
            index = mutation.get("index")
            if type(index) is not int or index not in targets:
                raise ValueError("Unknown caption review item.")
            if action == "undo":
                if edits.pop(str(index), None) is None:
                    raise ValueError("There is no manual correction to undo.")
            elif action == "save" and mutation.get("accepted") is True:
                start,end = mutation.get("start"),mutation.get("end")
                if any(type(n) not in (int,float) or not math.isfinite(n) for n in (start,end)):
                    raise ValueError("Enter finite start and end times in seconds.")
                edits[str(index)] = {"start": start, "end": end, "scene": script[index][0], "text": script[index][1]}
            else:
                raise ValueError("Listen and accept this timing before saving.")
    # Evaluate the final track in timestamp order to permit neighbouring manual edits without
    # validating a changed interval against a neighbour's obsolete base position.
    if edits:
        for key in edits:
            if int(key) not in targets:
                raise ValueError("A saved caption correction no longer refers to an approved word.")
        for key in edits:
            words.pop(int(key),None)
        for key, values in sorted(edits.items(),key=lambda item:int(item[0])):
            i = int(key)
            original = original_words.get(i) if i in review else None
            if original:
                words[i] = original
            apply(i, values["start"],values["end"])
    if mutation is not None:
        save(path, {"base": base, "timingKey": report.get("timingKey"), "edits": edits, "skips": skips})
        revision = digest(base, edits, windows, skips)

    unresolved = [{**item,"scriptIndex":i} for i,item in missing.items() if i not in words and str(i) not in skips]
    reviewed = [{**item,"scriptIndex":i} for i,item in review.items() if str(i) not in edits]
    ordered = [w for _,w in sorted(words.items())]
    remaining_openings = sorted(opening_reviews - {int(i) for i in edits})
    def _is_valid_suggestion(i, cand):
        if not isinstance(cand, dict):
            return False
        cs, ce = cand.get("start"), cand.get("end")
        if any(type(n) not in (int, float) or not math.isfinite(n) for n in (cs, ce)) or not cs < ce:
            return False
        prev_e = max((w["end"] for j, w in words.items() if j < i), default=0)
        next_s = min((w["start"] for j, w in words.items() if j > i), default=timeline["voiceDuration"])
        sc = scenes.get(script[i][0], {"start": 0, "end": timeline["voiceDuration"]})
        return max(prev_e, sc["start"]) <= cs < ce <= min(next_s, sc["end"], timeline["voiceDuration"])

    suggested_ids = sorted(
        {item["scriptIndex"] for item in reviewed if _is_valid_suggestion(item["scriptIndex"], item)}
        | {item["scriptIndex"] for item in unresolved if _is_valid_suggestion(item["scriptIndex"], item.get("candidate"))}
    )
    missing_ids = sorted({item["scriptIndex"] for item in unresolved} - set(suggested_ids))
    verified_count = sum(w["scriptWords"] for i, w in words.items() if str(i) in edits or i not in review)
    anchor_issues = []
    if skips:
        from motion_choreography import resolve_anchor_time

        skipped_scenes = {entry["scene"] for entry in skips.values()}
        for scene in plan["scenes"]:
            if scene["id"] not in skipped_scenes:
                continue
            storyboard = scene.get("storyboard") or {}
            scene_words = [word for word in ordered if word["scene"] == scene["id"]]
            for kind in ("model", "artwork"):
                for event in (storyboard.get(kind) or {}).get("events", []):
                    anchor = event.get("anchor")
                    if anchor and (not scene_words or resolve_anchor_time(
                            anchor, scene_words, scene["narration"], 0, timeline["voiceDuration"],
                            exact=True, occurrence=event.get("occurrence", 1)) is None):
                        anchor_issues.append(f'{scene["id"]}: {event["action"]} cue {anchor!r} has no verified onset. '
                                             'Restore the skipped word for timing or review the scene cue.')
    pending_indices = (required - {int(i) for i in edits}) | {row["scriptIndex"] for row in unresolved}

    effective = {**report, "unmatched":unresolved, "reviewWords":reviewed, "matches":ordered,
                 "coverage":sum(w["scriptWords"] for w in ordered)/len(script),
                 "captionRevision":revision, "automaticRecovery":list(automatic.values()),
                 "manualTimings":[words[int(i)] for i in edits],
                 "pendingSceneOpenings":remaining_openings,
                 "suggestedIndices": suggested_ids,
                 "missingIndices": missing_ids,
                 "verifiedCount": verified_count,
                 "skippedWords": [{"scriptIndex": int(i), **entry} for i, entry in sorted(skips.items(), key=lambda row: int(row[0]))],
                 "skippableIndices": [row["scriptIndex"] for row in unresolved if row["scriptIndex"] not in opening_reviews],
                 "remainingCount": len(pending_indices | set(remaining_openings)),
                 "anchorIssues": anchor_issues,
                 "premiumCaptionReady": not unresolved and not remaining_openings and not anchor_issues}
    if anchor_issues:
        effective["failureReason"] = anchor_issues[0]
    # Keep the established ASR strong-evidence metric and gate independent of manual review.
    if failed:
        pending = sorted(pending_indices | set(remaining_openings))
        supported = sum(w["scriptWords"] for w in ordered if w["match"] == "manual"
                        or (w["match"] != "approximate" and w.get("probability", 0) >= MIN_ANCHOR_CONFIDENCE))
        evidence = supported / max(len(script), report.get("recognizedWords", 0), 1)
        reason = (anchor_issues[0] if anchor_issues else
                  f"{len(pending)} entries still need verified timing." if pending else
                  f"Verified script evidence is {evidence:.1%}, below the {QUALITY_FLOOR:.0%} gate. "
                  "Check for extra speech or a recording that differs from the script."
                  if evidence < QUALITY_FLOOR else None)
        ready = reason is None
        effective.update(requiredTimingReviews=pending, verifiedCoverage=evidence, premiumCaptionReady=ready,
                         status="passed" if ready else "failed", failureReason=reason,
                         diagnosticGuidance=None)
        if not ready:
            return None, effective
        return measured_timeline(plan, timeline["voiceDuration"], ordered, report["timingKey"]), effective
    return {**timeline,"words":ordered}, effective


def current_caption_state(project, plan, mutation=None):
    """Load reviewed timing for production, or reviewable evidence for an incomplete alignment."""
    voice = project / "voice.wav"
    report = current_alignment_report(voice, plan)
    if report.get("status") == "failed" and report.get("timingKey") and report.get("scriptWords"):
        duration = report.get("voiceDuration")
        if type(duration) in (float, int) and math.isfinite(duration) and duration > 0:
            review = {"words":report.get("matches", []), "voiceDuration":duration, "scenes":[]}
            return caption_state(project, plan, review, report, mutation)
    path = project / "timeline.json"
    timeline = load(path) if path.is_file() else {}
    if timing_is_current(timeline, voice, plan):
        if mutation is not None and not report.get("scriptWords"):
            raise ValueError("A measured alignment report is required before saving a timing correction.")
        return caption_state(project, plan, timeline, report, mutation)
    if mutation is not None:
        raise ValueError("Align the current audio and script before saving timing corrections.")
    return None, report


def require_caption_timing(project, plan):
    timeline, report = current_caption_state(project, plan)
    if not timeline or report.get("premiumCaptionReady") is False:
        raise ValueError(report.get("failureReason") or
                         ("Finish the required alignment reviews before previewing or rendering."
                          if timeline else "Align the current narration before previewing or rendering."))
    return timeline, report
