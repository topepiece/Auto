#!/usr/bin/env python3
"""
Reels Auto-Poster
-----------------
Runs on GitHub Actions every 30 minutes. Each run it:
  1. checks if a reel is due (based on config.yaml)
  2. lets Gemini watch + listen to the next reel and write caption + hashtags
  3. posts it to Instagram with the official Instagram API
  4. remembers what was posted in state.json

You normally never need to edit this file. Edit config.yaml instead.
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state.json"
REELS_DIR = ROOT / "reels"
VIDEO_EXT = {".mp4", ".mov"}
IG_HOST = "https://graph.instagram.com"

ALERTS_TO_FAIL = []  # alerts that should make the run "fail" so GitHub emails you


# ----------------------------------------------------------------- helpers
def log(msg):
    print(msg, flush=True)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    defaults = {
        "niche": "general",
        "audience": "general audience",
        "language": "English",
        "tone": "friendly",
        "call_to_action": "end with a short question",
        "hashtag_count": 8,
        "always_hashtags": [],
        "avoid_words": [],
        "timezone": "UTC",
        "posts_per_day": 3,
        "window_start": "09:00",
        "window_end": "21:00",
        "post_times": [],
        "catchup_minutes": 240,
        "low_queue_warn": 5,
        "token_warn_days": 50,
        "fallback_after_minutes": 120,
        "prepare_ahead": 3,
        "fallback_caption": "New reel! Tell me what you think.",
        "fallback_hashtags": ["reels"],
        "gemini_models": ["gemini-flash-latest", "gemini-flash-lite-latest"],
        "video_base_url": "",
        "graph_version": "",
    }
    for k, v in defaults.items():
        if cfg.get(k) is None:
            cfg[k] = v
    return cfg


def load_state():
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    data.setdefault("posted", {})
    data.setdefault("slots_done", {})
    data.setdefault("attempts", {})
    data.setdefault("alerts", {})
    data.setdefault("captions", {})
    return data


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def alert(state, key, message, today):
    """Show a warning. Make the run fail at most once per day per alert
    (a failed run makes GitHub send you an email)."""
    log(f"::warning::{message}")
    if state["alerts"].get(key) != today:
        state["alerts"][key] = today
        ALERTS_TO_FAIL.append(message)


def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def hhmm_to_minutes(s):
    h, m = str(s).strip().split(":")
    return int(h) * 60 + int(m)


def minutes_to_hhmm(total):
    return f"{total // 60:02d}:{total % 60:02d}"


# ---------------------------------------------------------------- schedule
def compute_slots(cfg):
    """Return the list of daily posting times as 'HH:MM' strings."""
    times = cfg.get("post_times") or []
    if times:
        return sorted({minutes_to_hhmm(hhmm_to_minutes(t)) for t in times})
    n = max(1, int(cfg["posts_per_day"]))
    start = hhmm_to_minutes(cfg["window_start"])
    end = hhmm_to_minutes(cfg["window_end"])
    if n == 1:
        return [minutes_to_hhmm(start)]
    step = (end - start) / (n - 1)
    return [minutes_to_hhmm(round(start + i * step)) for i in range(n)]


def find_due_slot(cfg, state, now_local):
    """Return the 'HH:MM' slot that should be posted now, or None."""
    today = now_local.strftime("%Y-%m-%d")
    done = set(state["slots_done"].get(today, []))
    catchup = timedelta(minutes=int(cfg["catchup_minutes"]))
    for slot in compute_slots(cfg):
        if slot in done:
            continue
        h, m = slot.split(":")
        slot_dt = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if slot_dt <= now_local < slot_dt + catchup:
            return slot
    return None


def mark_slot_done(state, now_local, slot):
    today = now_local.strftime("%Y-%m-%d")
    state["slots_done"].setdefault(today, []).append(slot)
    # keep only the last 4 days
    for d in sorted(state["slots_done"])[:-4]:
        del state["slots_done"][d]


# ------------------------------------------------------------------- queue
def list_queue(state):
    if not REELS_DIR.exists():
        return []
    names = [
        p.name
        for p in REELS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXT and p.name not in state["posted"]
    ]
    return sorted(names, key=natural_key)


def video_url_for(cfg, name):
    base = (cfg.get("video_base_url") or "").rstrip("/")
    if not base:
        repo_full = os.environ.get("GITHUB_REPOSITORY", "")
        if "/" not in repo_full:
            raise RuntimeError(
                "Cannot work out the public video address. Set video_base_url in config.yaml."
            )
        owner, repo = repo_full.split("/", 1)
        if repo.lower() == f"{owner.lower()}.github.io":
            base = f"https://{owner.lower()}.github.io"
        else:
            base = f"https://{owner.lower()}.github.io/{repo}"
    return f"{base}/reels/{quote(name)}"


def video_is_online(url, expected_size):
    """GitHub Pages needs a minute or two after you upload a file.
    Check that the video really is reachable and complete."""
    try:
        r = requests.get(url, stream=True, timeout=30, headers={"Range": "bytes=0-1"})
        ok = r.status_code in (200, 206)
        ctype = r.headers.get("Content-Type", "")
        total = None
        cr = r.headers.get("Content-Range", "")
        if "/" in cr:
            total = int(cr.split("/")[-1])
        elif r.headers.get("Content-Length") and r.status_code == 200:
            total = int(r.headers["Content-Length"])
        r.close()
        if not ok:
            log(f"  not online yet (HTTP {r.status_code}): {url}")
            return False
        if "text/html" in ctype:
            log(f"  address returns a web page, not a video: {url}")
            return False
        if total is not None and expected_size and total != expected_size:
            log(f"  video still uploading/deploying (size {total} vs {expected_size})")
            return False
        return True
    except requests.RequestException as e:
        log(f"  could not reach {url}: {e}")
        return False


# --------------------------------------------------------------- AI caption
def build_prompt(cfg):
    avoid = ", ".join(cfg["avoid_words"]) if cfg["avoid_words"] else "none"
    return f"""You are an expert Instagram Reels growth writer.

PAGE NICHE: {cfg['niche']}
AUDIENCE: {cfg['audience']}
LANGUAGE: {cfg['language']}
TONE: {cfg['tone']}

Watch this video carefully AND listen to its audio (speech, music, sounds, any on-screen text).
Then write the post text for Instagram.

Return ONLY valid JSON with exactly these keys:
{{
  "summary": "one plain sentence: what really happens / is said in this reel",
  "hook": "first line of the caption, max 80 characters, makes people stop scrolling",
  "caption": "1 to 3 short sentences about the reel, then: {cfg['call_to_action']}",
  "hashtags": ["exactly {cfg['hashtag_count']} hashtags WITHOUT the # symbol"]
}}

Rules:
- Only describe things that are really in the video. Do not invent facts, names, prices or promises.
- Write in {cfg['language']}. Use simple words. At most 2 emojis in total.
- Hashtags: about 1/3 broad (big topic), the rest specific to this exact reel and the niche. No spaces inside a tag.
- Never use spammy tags like follow4follow, like4like, followforfollow, viral4you.
- Do not put hashtags inside the caption text.
- Never use these words: {avoid}.
- Do not mention file names.
"""


def parse_json_loose(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def clean_hashtag(tag):
    tag = str(tag).strip().lstrip("#")
    tag = re.sub(r"[^\w]", "", tag, flags=re.UNICODE)
    return tag


BANNED_TAGS = {"follow4follow", "like4like", "followforfollow", "likeforlike", "f4f", "l4l"}


def assemble_caption(cfg, data):
    hook = str(data.get("hook", "")).strip()
    body = str(data.get("caption", "")).strip()
    tags = []
    for t in list(data.get("hashtags", [])) + list(cfg["always_hashtags"]):
        t = clean_hashtag(t)
        if t and t.lower() not in BANNED_TAGS and t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    tags = tags[:30]  # Instagram allows max 30
    parts = [p for p in (hook, body) if p]
    text = "\n\n".join(parts)
    if tags:
        text += "\n\n" + " ".join("#" + t for t in tags)
    return text[:2200].strip()


def fallback_caption(cfg):
    tags = [clean_hashtag(t) for t in (cfg["fallback_hashtags"] + cfg["always_hashtags"])]
    tags = [t for t in dict.fromkeys(tags) if t]
    return (cfg["fallback_caption"] + "\n\n" + " ".join("#" + t for t in tags)).strip()


def generate_caption_with_gemini(cfg, video_path, api_key):
    from google import genai  # imported here so tests do not need it
    from google.genai import types

    client = genai.Client(api_key=api_key)
    log(f"  uploading {video_path.name} to Gemini ...")
    uploaded = client.files.upload(file=str(video_path))
    try:
        for _ in range(60):  # wait up to ~5 minutes for processing
            state_name = getattr(uploaded.state, "name", str(uploaded.state))
            if state_name == "ACTIVE":
                break
            if state_name == "FAILED":
                raise RuntimeError("Gemini could not process this video")
            time.sleep(5)
            uploaded = client.files.get(name=uploaded.name)
        prompt = build_prompt(cfg)
        last_err = None
        for model in cfg["gemini_models"]:
            for attempt in range(3):
                try:
                    log(f"  asking {model} (try {attempt + 1}) ...")
                    resp = client.models.generate_content(
                        model=model,
                        contents=[uploaded, prompt],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json", temperature=0.9
                        ),
                    )
                    data = parse_json_loose(resp.text)
                    log(f"  AI summary: {data.get('summary', '')}")
                    return assemble_caption(cfg, data)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    msg = str(e)
                    log(f"  AI problem: {msg[:200]}")
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        time.sleep(30)
                        continue
                    if "404" in msg or "NOT_FOUND" in msg:
                        break  # this model name does not exist, try next model
                    time.sleep(5)
        raise RuntimeError(f"All AI models failed: {last_err}")
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:  # noqa: BLE001
            pass



def prepare_captions(cfg, state, queue, api_key, max_new=1):
    """Write captions IN ADVANCE for the next few reels, at quiet times.
    If Google is busy at posting time, the caption is already waiting."""
    if not api_key:
        return
    made = 0
    for name in queue[: int(cfg["prepare_ahead"])]:
        if made >= max_new:
            break
        if name in state["captions"]:
            continue
        try:
            log(f"Preparing caption in advance for {name} ...")
            state["captions"][name] = generate_caption_with_gemini(cfg, REELS_DIR / name, api_key)
            made += 1
        except Exception as e:  # noqa: BLE001
            log(f"  could not prepare now (will try again next run): {str(e)[:150]}")
            break


# --------------------------------------------------------------- Instagram
def ig_url(cfg, path):
    ver = (cfg.get("graph_version") or "").strip("/")
    return f"{IG_HOST}/{ver}/{path}" if ver else f"{IG_HOST}/{path}"


def ig_check(resp):
    try:
        j = resp.json()
    except ValueError:
        j = {"raw": resp.text[:300]}
    if resp.status_code >= 400 or "error" in j:
        raise RuntimeError(f"Instagram said: {json.dumps(j)[:500]}")
    return j


def ig_get_user_id(cfg, token):
    override = os.environ.get("IG_USER_ID", "").strip()
    if override:
        return override
    r = requests.get(
        ig_url(cfg, "me"),
        params={"fields": "user_id,username"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    j = ig_check(r)
    uid = j.get("user_id") or j.get("id")
    log(f"  Instagram account: @{j.get('username', '?')}")
    return str(uid)


def ig_publish_reel(cfg, token, ig_id, video_url, caption):
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.post(
        ig_url(cfg, f"{ig_id}/media"),
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
        },
        headers=headers,
        timeout=60,
    )
    container = ig_check(r)["id"]
    log(f"  container created: {container}")
    for i in range(60):  # up to ~10 minutes
        time.sleep(10)
        s = requests.get(
            ig_url(cfg, container),
            params={"fields": "status_code,status"},
            headers=headers,
            timeout=30,
        )
        j = ig_check(s)
        code = j.get("status_code")
        log(f"  processing: {code}")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video: {j}")
    else:
        raise RuntimeError("Instagram took too long to process the video")
    p = requests.post(
        ig_url(cfg, f"{ig_id}/media_publish"),
        data={"creation_id": container},
        headers=headers,
        timeout=60,
    )
    return ig_check(p)["id"]


# -------------------------------------------------------------------- main
def check_token_age(cfg, state, token, today):
    fp = hashlib.sha256(token.encode()).hexdigest()[:12]
    if state.get("token_fp") != fp:
        state["token_fp"] = fp
        state["token_since"] = today
        log("  new Instagram token detected - age counter reset")
    since = date.fromisoformat(state.get("token_since", today))
    age = (date.fromisoformat(today) - since).days
    if age >= int(cfg["token_warn_days"]):
        alert(
            state,
            "token_old",
            f"Your Instagram token is {age} days old and expires at 60 days. "
            "Make a new one (see the guide, Part 10) and paste it into the IG_ACCESS_TOKEN secret.",
            today,
        )


def main():
    force = os.environ.get("FORCE", "").lower() in ("true", "1", "yes")
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes")

    cfg = load_config()
    state = load_state()
    try:
        tz = ZoneInfo(cfg["timezone"])
    except Exception:  # noqa: BLE001
        log(f"::error::Timezone '{cfg['timezone']}' in config.yaml is not valid.")
        return 2
    now_local = datetime.now(tz)
    today = now_local.strftime("%Y-%m-%d")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    log(f"Local time now: {now_local:%Y-%m-%d %H:%M} ({cfg['timezone']})")
    log(f"Daily slots: {', '.join(compute_slots(cfg))}")

    def finish(code_if_ok=0):
        """Use spare time to write captions for the next reels, then save."""
        try:
            prepare_captions(cfg, state, list_queue(state), api_key)
        except Exception as e:  # noqa: BLE001
            log(f"  (prepare step skipped: {str(e)[:120]})")
        save_state(state)
        return 1 if ALERTS_TO_FAIL else code_if_ok

    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if token:
        check_token_age(cfg, state, token, today)

    queue = list_queue(state)
    log(f"Reels waiting: {len(queue)}")
    if not queue:
        alert(state, "queue_empty", "No reels left. Upload more reels into the 'reels' folder.", today)
        save_state(state)
        return 1 if ALERTS_TO_FAIL else 0
    if len(queue) <= int(cfg["low_queue_warn"]):
        alert(state, "queue_low", f"Only {len(queue)} reels left. Upload more soon.", today)

    slot = None
    minutes_late = 0
    if force:
        log("FORCE mode: posting the next reel now.")
    else:
        slot = find_due_slot(cfg, state, now_local)
        if slot is None:
            log("Nothing due right now. Done.")
            return finish()
        h, m = slot.split(":")
        slot_dt = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        minutes_late = int((now_local - slot_dt).total_seconds() // 60)
        log(f"Slot {slot} is due ({minutes_late} min ago).")

    # pick the first reel that is online and has not failed 3 times
    chosen = None
    for name in queue:
        if state["attempts"].get(name, 0) >= 3:
            alert(state, f"skip_{name}", f"{name} failed 3 times and is being skipped. Check or replace it.", today)
            continue
        url = video_url_for(cfg, name)
        log(f"Checking {name} ...")
        if video_is_online(url, (REELS_DIR / name).stat().st_size):
            chosen = (name, url)
            break
        log("  skipping for now (GitHub Pages may still be publishing it).")
    if not chosen:
        log("No reel is ready to post yet. Will try again next run.")
        return finish()
    name, url = chosen

    # caption: use the one prepared in advance, otherwise write it now
    caption = state["captions"].get(name)
    if caption:
        log("  using the caption that was prepared in advance")
    elif api_key:
        try:
            caption = generate_caption_with_gemini(cfg, REELS_DIR / name, api_key)
            state["captions"][name] = caption
        except Exception as e:  # noqa: BLE001
            log(f"  AI caption failed: {e}")
    else:
        log("  no GEMINI_API_KEY set")
    if not caption:
        wait_limit = int(cfg["fallback_after_minutes"])
        if not force and minutes_late < wait_limit:
            log(f"  Google AI is busy. Waiting and retrying next run (backup caption is used only after {wait_limit} min).")
            return finish()
        if force:
            log("  AI failed. Run again in a few minutes.")
            return finish()
        caption = fallback_caption(cfg)
        log("  AI stayed busy too long, using the backup caption")

    log("----- CAPTION -----")
    log(caption)
    log("-------------------")

    if dry_run:
        log("DRY RUN: nothing was posted.")
        return finish()

    if not token:
        log("::error::IG_ACCESS_TOKEN secret is missing.")
        return 2

    try:
        ig_id = ig_get_user_id(cfg, token)
        log("Publishing to Instagram ...")
        media_id = ig_publish_reel(cfg, token, ig_id, url, caption)
    except Exception as e:  # noqa: BLE001
        state["attempts"][name] = state["attempts"].get(name, 0) + 1
        alert(state, "post_failed", f"Posting {name} failed: {e}", today)
        save_state(state)
        return 1

    state["posted"][name] = {
        "at": datetime.now(tz).isoformat(timespec="seconds"),
        "media_id": media_id,
        "slot": slot or "manual",
        "caption": caption,
    }
    state["captions"].pop(name, None)
    state["attempts"].pop(name, None)
    if slot:
        mark_slot_done(state, now_local, slot)
    save_state(state)  # save immediately so we never post the same reel twice
    log(f"POSTED {name} -> Instagram media id {media_id}")
    return finish()


if __name__ == "__main__":
    code = main()
    if ALERTS_TO_FAIL:
        log("\nATTENTION NEEDED:\n - " + "\n - ".join(ALERTS_TO_FAIL))
    sys.exit(code)
