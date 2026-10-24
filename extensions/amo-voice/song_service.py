#!/usr/bin/env python3
"""Amo singing pipeline: find song -> UVR -> RVC -> mix -> WeChat voice."""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import librosa
import numpy as np
import soundfile as sf

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from vocal_check import analyze_audio

RVC_ROOT = Path(r"E:\RVC\app\RVC20260718Nvidia")
RVC_PY = RVC_ROOT / "runtime" / "python.exe"
FFMPEG = RVC_ROOT / "ffmpeg.exe"
FFPROBE = RVC_ROOT / "ffprobe.exe"
VOICE_URL = "http://127.0.0.1:8790"
OUT_ROOT = Path(r"E:\DSH\AmoData\songs")
MUSIC_DIR = Path(r"C:\Users\User\Music")
RVC_OUT = Path(r"E:\DSH\AmoData\voice-out\rvc")
MODEL_DIR = RVC_ROOT / "assets" / "weights"
PORT = 8898

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


WORKSPACE = BASE.parent.parent
HOOK_TOKEN_FILE = WORKSPACE / "state" / "hook" / "native-token.txt"
IO_CONFIG_FILE = WORKSPACE / "state" / "wechat-io-config.json"
PROJECT_CONFIG_FILE = WORKSPACE / "config" / "wechatagent.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _http_json(url: str, body: dict | None = None, headers: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if body is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _safe_file_name(value: str) -> str:
    name = "".join(ch for ch in value if ch not in '\\/:*?"<>|').strip()
    return name[:120] or "Amo-song"


def send_song_file(path: Path, query: str = "", chat_name: str = "Amon") -> str:
    """Send a full-quality WAV through the native WeChat file hook."""
    io = _read_json(IO_CONFIG_FILE)
    reader_base = str(io["reader_base_url"]).rstrip("/")
    reader_token = str(io["reader_token"])
    sessions = _http_json(reader_base + "/api/v1/sessions?limit=500",
                          headers={"Authorization": "Bearer " + reader_token}).get("sessions", [])
    target = next((row for row in sessions if row.get("displayName") == chat_name), None)
    if target is None:
        target = next((row for row in sessions if chat_name in str(row.get("displayName", ""))), None)
    if not target or not target.get("username"):
        raise RuntimeError("未找到微信会话: " + chat_name)
    wxid = str(target["username"])

    project = _read_json(PROJECT_CONFIG_FILE)
    hook_base = str(project["hook"]["baseUrl"]).rstrip("/")
    native_token = HOOK_TOKEN_FILE.read_text(encoding="utf-8").strip()
    health = _http_json(hook_base + "/WeChatAgent/health",
                        headers={"Authorization": "Bearer " + native_token})
    accounts = health.get("databaseAccounts") or []
    if not accounts:
        raise RuntimeError("微信文件发送账号未就绪")
    account = str(accounts[0])

    stage_dir = WORKSPACE / "state" / "files" / "outgoing" / (uuid.uuid4().hex + uuid.uuid4().hex)
    stage_dir.mkdir(parents=True, exist_ok=False)
    label = _safe_file_name("Amo-" + (query or "song") + "-" + datetime.now().strftime("%H%M%S"))
    staged = stage_dir / (label + ".wav")
    shutil.copy2(path, staged)
    md5 = hashlib.md5(staged.read_bytes()).hexdigest()
    size = staged.stat().st_size
    result = _http_json(hook_base + "/SendFileMsg",
                        body={"wxidorgid": wxid, "path": str(staged), "md5": md5, "size": size},
                        headers={"Authorization": "Bearer " + native_token,
                                 "X-WeChatAgent-Account": account})
    if result.get("ret") != 0:
        raise RuntimeError("微信文件发送失败: " + str(result))
    return str(staged)


def file_url(path: str | Path) -> str:
    return "/singing-file?path=" + urllib.parse.quote(str(path), safe="")


def find_local(query: str) -> str | None:
    terms = [t for t in query.lower().replace("_", " ").split() if t]
    candidates = []
    for root in (MUSIC_DIR, OUT_ROOT):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".mp3", ".flac", ".m4a", ".wav", ".ogg"}:
                continue
            text = path.name.lower()
            score = sum(1 for t in terms if t in text)
            if score:
                candidates.append((score, len(text), path))
    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return str(candidates[0][2])
    return None


def detect_source_gender(path: str) -> str:
    name = Path(path).name.lower()
    male_hints = ('逃跑计划', '米津玄師', '米津玄师', 'jay', '周杰伦')
    female_hints = ('浅影阿', 'ヨルシカ', 'jimmy', 'clean bandit')
    if any(token.lower() in name for token in male_hints):
        return 'male'
    if any(token.lower() in name for token in female_hints):
        return 'female'
    try:
        y, sr = librosa.load(path, sr=24000, mono=True)
        f0 = librosa.pyin(y, fmin=60, fmax=500, sr=sr, frame_length=2048, hop_length=512)[0]
        f0 = f0[~np.isnan(f0)]
        if len(f0):
            median = float(np.median(f0))
            return 'male' if median < 130.0 else 'female'
    except Exception:
        pass
    return 'female'


def choose_vocal_start(path: Path, duration: float, fallback: float = 0.0) -> float:
    """Pick the window with the strongest sustained vocal energy."""
    try:
        y, sr = librosa.load(str(path), sr=16000, mono=True)
        win = int(max(1.0, duration) * sr)
        hop = max(1, int(0.5 * sr))
        if len(y) <= win:
            return 0.0
        best_start, best_score = int(max(0.0, fallback) * sr), -1.0
        for start in range(0, len(y) - win, hop):
            seg = y[start:start + win]
            rms = float(np.mean(librosa.feature.rms(y=seg, frame_length=1024, hop_length=256)))
            if rms > best_score:
                best_score, best_start = rms, start
        return round(best_start / sr, 2)
    except Exception:
        return float(fallback)


def _song_candidates(female_only: bool = False) -> list[Path]:
    if not MUSIC_DIR.exists():
        return []
    blocked = ('纯音乐', 'instrumental', 'theme', 'elden ring', 'leyndell', 'his theme',
               'forest mixtape', 'seto-luv', 'bloodseeker', 'isekai phonk')
    female_hints = ('浅影阿', 'ヨルシカ', 'jimmy', 'clean bandit')
    rows = []
    for path in MUSIC_DIR.iterdir():
        if path.suffix.lower() not in {'.mp3', '.flac', '.m4a', '.wav'}:
            continue
        name = path.name.lower()
        if any(token in name for token in blocked):
            continue
        if female_only and not any(token in name for token in female_hints):
            continue
        rows.append(path)
    return sorted(rows, key=lambda p: p.name)


def pick_random_song() -> str | None:
    songs = _song_candidates(female_only=True) or _song_candidates()
    if not songs:
        return None
    state_file = OUT_ROOT / 'random-state.json'
    last = ''
    try:
        last = str(json.loads(state_file.read_text(encoding='utf-8')).get('last', ''))
    except Exception:
        pass
    choices = [p for p in songs if p.name != last] or songs
    chosen = random.choice(choices)
    try:
        OUT_ROOT.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({'last': chosen.name}, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass
    return str(chosen)


def add_step(job: dict[str, Any], key: str, title: str, status: str = "running", path: str = "", detail: str = "") -> dict[str, Any]:
    step = next((s for s in job["steps"] if s["key"] == key), None)
    if step is None:
        step = {"key": key, "title": title, "status": status, "path": "", "url": "", "detail": ""}
        job["steps"].append(step)
    step.update({"status": status, "detail": detail})
    if path:
        step["path"] = str(path)
        step["url"] = file_url(path)
    job["updated"] = now()
    return step


def run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout)


def latest_wav(folder: Path) -> Path | None:
    files = sorted(folder.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []
    return files[0] if files else None


def run_job(job_id: str, body: dict[str, Any]) -> None:
    job = JOBS[job_id]
    job_dir = OUT_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        add_step(job, "find", "找歌")
        query = str(body.get("query", "")).strip()
        source = str(body.get("source", "")).strip()
        wants_random = bool(body.get("random")) or not query
        if source and Path(source).is_file():
            pass
        else:
            source = (pick_random_song() if wants_random else None) or find_local(query) or ""
        if not source:
            # Bilibili fallback: yt-dlp search + download audio.
            add_step(job, "find", "找歌", "running", detail=f"本地没找到「{query}」，尝试 B 站")
            outtmpl = str(job_dir / "source.%(ext)s")
            result = run([
                str(RVC_PY), "-m", "yt_dlp", f"bilisearch1:{query}",
                "-f", "bestaudio", "-x", "--audio-format", "wav",
                "--ffmpeg-location", str(RVC_ROOT), "-o", outtmpl,
                "--no-warnings",
            ], timeout=600)
            found = sorted(job_dir.glob("source.*"))
            if not found:
                raise RuntimeError("没有找到歌曲；可在本机音乐库选择，或把 B 站链接发我。")
            source = str(found[0])
        add_step(job, "find", "找歌", "done", source, Path(source).name)

        add_step(job, "separate", "分离人声/伴奏")
        input_dir = job_dir / "input"
        input_dir.mkdir(exist_ok=True)
        input_wav = input_dir / "song.wav"
        conv = run([str(FFMPEG), "-y", "-i", source, "-vn", "-ac", "2", "-ar", "44100", str(input_wav)], timeout=300)
        if conv.returncode != 0:
            raise RuntimeError("音频转换失败: " + conv.stderr[-300:])
        vocal_dir = job_dir / "vocal"
        inst_dir = job_dir / "instrumental"
        vocal_dir.mkdir(exist_ok=True)
        inst_dir.mkdir(exist_ok=True)
        env = os.environ.copy()
        env.update({
            "UVR_INPUT": str(input_wav),
            "UVR_VOCAL": str(vocal_dir),
            "UVR_INSTRUMENTAL": str(inst_dir),
            "UVR_MODEL": "model_bs_roformer_ep_317_sdr_12.9755",
        })
        uvr = run([str(RVC_PY), str(BASE / "uvr_once.py")], env=env, timeout=900)
        vocal = latest_wav(vocal_dir)
        inst = latest_wav(inst_dir)
        if vocal is None or inst is None:
            raise RuntimeError("UVR 没有产出人声/伴奏: " + (uvr.stderr or uvr.stdout)[-300:])
        add_step(job, "separate", "分离人声/伴奏", "done", str(vocal), f"人声 {vocal.name}")
        job["instrumental"] = str(inst)

        add_step(job, "convert", "RVC 换声")
        duration = min(30.0, max(5.0, float(body.get("duration", 20))))
        try:
            requested_start = max(0.0, float(body.get("start", 35)))
        except Exception:
            requested_start = 35.0
        auto_start = bool(body.get("auto_start"))
        start = choose_vocal_start(vocal, duration, requested_start) if auto_start else requested_start
        job["start"] = start
        vocal_seg = job_dir / "vocal-segment.wav"
        inst_seg = job_dir / "instrumental-segment.wav"
        for src, dst, ac in ((vocal, vocal_seg, "1"), (inst, inst_seg, "2")):
            cut = run([str(FFMPEG), "-y", "-ss", str(start), "-t", str(duration), "-i", str(src), "-ac", ac, "-ar", "44100", str(dst)], timeout=180)
            if cut.returncode != 0:
                raise RuntimeError("截取片段失败: " + cut.stderr[-300:])

        add_step(job, "detect", "检测人声/伴奏")
        try:
            vocal_stats = analyze_audio(str(vocal_seg))
            inst_stats = analyze_audio(str(inst_seg))
            vocal_pct = round(float(vocal_stats.get("vocal_score", 0)) * 100)
            music_pct = round(float(inst_stats.get("music_score", 0)) * 100)
            leak_pct = round(float(inst_stats.get("vocal_band_ratio", 0)) * 100)
            add_step(job, "detect", "检测人声/伴奏", "done",
                     detail=f"人声活动 {vocal_pct}% / 伴奏 {music_pct}% / 人声频段残留 {leak_pct}%")
        except Exception as error:
            add_step(job, "detect", "检测人声/伴奏", "done", detail=f"检测跳过: {error}")

        rvc_out = job_dir / "rvc.wav"
        model = str(body.get("model", "")).strip()
        gender = str(body.get("gender", "auto")).strip() or "auto"
        if gender == "auto":
            gender = "female" if wants_random else detect_source_gender(source)
        rvc_cmd = [str(RVC_PY), str(BASE / "rvc_sing.py"), "--source", str(vocal_seg), "--gender", gender, "--output", str(rvc_out)]
        if model:
            rvc_cmd += ["--model", model]
        rvc = run(rvc_cmd, timeout=600)
        if rvc.returncode != 0 or not rvc_out.exists():
            raise RuntimeError("RVC 换声失败: " + (rvc.stderr or rvc.stdout)[-400:])
        auto_note = "pm / protect 0.15 / rms 0.35 / peak 0.5"
        try:
            rvc_audio, _ = sf.read(str(rvc_out), always_2d=False)
            rvc_peak = float(np.max(np.abs(rvc_audio))) if len(rvc_audio) else 0.0
            if rvc_peak >= 0.95:
                fallback_cmd = [str(RVC_PY), str(BASE / "rvc_sing.py"), "--source", str(vocal_seg), "--gender", gender,
                                "--output", str(rvc_out), "--protect", "0.2", "--rms-mix", "0.3", "--peak", "0.4"]
                if model:
                    fallback_cmd += ["--model", model]
                fallback = run(fallback_cmd, timeout=600)
                if fallback.returncode == 0 and rvc_out.exists():
                    auto_note = "自动兜底 / pm / protect 0.2 / rms 0.3 / peak 0.4"
        except Exception:
            pass
        add_step(job, "convert", "RVC 换声", "done", str(rvc_out), f"{model or '默认模型'} / speaker 0 / {gender} / pitch {'+8' if gender == 'male' else '0'} / {auto_note}")

        add_step(job, "mix", "混音")
        mixed = job_dir / "mixed.wav"
        mix = run([
            str(FFMPEG), "-y", "-i", str(rvc_out), "-i", str(inst_seg),
            "-filter_complex", "[0:a]aresample=48000,volume=1.45[v];[1:a]volume=0.45[m];[v][m]amix=inputs=2:duration=shortest,loudnorm=I=-20:TP=-2:LRA=9",
            "-t", str(duration), "-ar", "44100", "-ac", "1", str(mixed),
        ], timeout=300)
        if mix.returncode != 0 or not mixed.exists():
            raise RuntimeError("混音失败: " + mix.stderr[-300:])
        add_step(job, "mix", "混音", "done", str(mixed), f"{duration:.0f}s / 起点 {start:.1f}s / 人声 1.45 / 伴奏 0.45")

        mode = str(body.get("send_mode", "file") or "file").strip().lower()
        send_title = "微信语音条" if mode == "voice" else "微信文件"
        add_step(job, "send", send_title)
        if body.get("send", True):
            if mode == "voice":
                req = urllib.request.Request(
                    VOICE_URL + "/send_audio",
                    data=json.dumps({"path": str(mixed), "dry_run": False}).encode("utf-8"),
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=300) as response:
                    reply = json.loads(response.read().decode("utf-8"))
                processed = reply.get("path", "")
                add_step(job, "send", send_title, "done", processed, f"{reply.get('duration', 0):.1f}s")
            else:
                staged = send_song_file(mixed, str(body.get("query", "")), "Amon")
                add_step(job, "send", send_title, "done", staged, "WAV 文件")
        else:
            add_step(job, "send", send_title, "skipped", detail="仅生成，未发送")
        job["status"] = "done"
        job["finished"] = now()
    except Exception as error:
        job["status"] = "failed"
        job["error"] = str(error)
        job["finished"] = now()
        for step in job["steps"]:
            if step["status"] == "running":
                step["status"] = "failed"
                step["detail"] = str(error)
    finally:
        try:
            (job_dir / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "AmoSinging/0.1"

    def _json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self) -> None:
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _library(self) -> list[dict[str, str]]:
        rows = []
        if MUSIC_DIR.exists():
            for path in MUSIC_DIR.iterdir():
                if path.suffix.lower() in {".mp3", ".flac", ".m4a", ".wav"}:
                    rows.append({"name": path.name, "path": str(path)})
        rows.sort(key=lambda row: row["name"])
        return rows[:200]

    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        if url.path in {"/", "/singing"}:
            self._html()
            return
        if url.path == "/singing-api/library":
            self._json(200, {"songs": self._library()})
            return
        if url.path == "/singing-api/models":
            models = sorted(MODEL_DIR.glob("*.pth")) if MODEL_DIR.exists() else []
            self._json(200, {"models": [{"name": p.name, "path": str(p)} for p in models]})
            return
        if url.path == "/singing-api/open-folder":
            query = urllib.parse.parse_qs(url.query)
            path = Path((query.get("path") or [""])[0]).resolve()
            allowed = [MODEL_DIR.resolve(), MUSIC_DIR.resolve(), OUT_ROOT.resolve(), RVC_ROOT.resolve(), Path(r"F:\音频参考").resolve()]
            if not any(path == root or root in path.parents for root in allowed) or not path.exists():
                self._json(404, {"ok": False, "error": "path not allowed"})
                return
            target = path if path.is_dir() else path.parent
            subprocess.Popen(["explorer.exe", str(target)])
            self._json(200, {"ok": True, "path": str(target)})
            return
        if url.path == "/singing-api/status":
            query = urllib.parse.parse_qs(url.query)
            job_id = (query.get("id") or [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            self._json(200, job or {"status": "idle"})
            return
        if url.path == "/singing-file":
            query = urllib.parse.parse_qs(url.query)
            raw = (query.get("path") or [""])[0]
            path = Path(raw).resolve()
            allowed = [OUT_ROOT.resolve(), MUSIC_DIR.resolve(), Path(r"E:\DSH\AmoData\voice-out").resolve(), Path(r"D:\gpt\voice-out").resolve()]
            if not any(path == root or root in path.parents for root in allowed) or not path.is_file():
                self._json(404, {"error": "not found"})
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/singing-api/run":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"error": "invalid json"})
            return
        if not RUN_LOCK.acquire(blocking=False):
            self._json(409, {"error": "已有任务在跑"})
            return
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        job = {
            "id": job_id,
            "status": "running",
            "started": now(),
            "steps": [],
            "query": body.get("query", ""),
        }
        with JOBS_LOCK:
            JOBS[job_id] = job
        thread = threading.Thread(target=lambda: (run_job(job_id, body), RUN_LOCK.release()), daemon=True)
        thread.start()
        self._json(202, {"id": job_id})


HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Amo 唱歌</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#1e293b;font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:28px 20px 60px}h1{font-size:24px;margin:0 0 6px}p.sub{color:#64748b;margin:0 0 22px}
.panel{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:18px;margin-bottom:16px;box-shadow:0 2px 8px rgba(15,23,42,.04)}
label{display:block;font-size:12px;color:#64748b;margin:0 0 5px}select,input{width:100%;padding:9px 10px;border:1px solid #cbd5e1;border-radius:8px;font:inherit;background:#fff}
.row{display:flex;flex-direction:column;gap:10px}button{border:0;border-radius:9px;background:#0f766e;color:#fff;padding:10px 18px;font:inherit;cursor:pointer}
button:disabled{opacity:.5;cursor:default}.hint{color:#94a3b8;font-size:12px;margin-top:8px}.folders{display:flex;flex-direction:column;gap:6px;margin-top:12px}.folders button{background:#e2e8f0;color:#334155;padding:8px 10px;text-align:left}.folder-btn{align-self:flex-start;background:#e2e8f0;color:#334155;padding:6px 9px;font-size:12px;margin-top:2px;text-align:left}
.steps{display:grid;gap:10px}.step{display:flex;align-items:center;gap:12px;border:1px solid #e2e8f0;border-radius:10px;padding:12px;background:#fff}
.step .n{width:26px;height:26px;border-radius:50%;background:#e2e8f0;color:#475569;display:flex;align-items:center;justify-content:center;font-size:12px;flex:none}
.step.done .n{background:#ccfbf1;color:#0f766e}.step.failed .n{background:#fee2e2;color:#b91c1c}.step.running .n{background:#fef3c7;color:#92400e}
.step .title{font-weight:600}.step .detail{color:#64748b;font-size:12px}.step a{margin-left:auto;color:#0f766e;text-decoration:none;font-weight:600;white-space:nowrap}
.badge{font-size:12px;color:#64748b}.err{color:#b91c1c;white-space:pre-wrap}.audio{width:100%;margin-top:8px}
</style></head><body><div class="wrap">
<h1>唱歌</h1><p class="sub">找歌 → 分离人声/伴奏 → RVC 换声 → 混音 → 微信 WAV 文件。每一步都可以点开查看。</p>
<div class="panel"><div class="row"><div class="field"><label>歌曲</label><select id="song"></select><button class="folder-btn" data-folder="music">打开歌曲文件夹</button></div><div class="field"><label>RVC 模型</label><select id="model"></select><button class="folder-btn" data-folder="models">打开 RVC 模型文件夹</button></div><div class="field"><label>开始秒</label><input id="start" type="number" value="35"></div><div class="field"><label>时长秒</label><input id="duration" type="number" value="20" min="5" max="30"></div></div>
<div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap"><button id="run">开始唱歌</button><div class="field" style="margin:0;min-width:150px"><label>发送方式</label><select id="sendMode"><option value="file">文件（高音质）</option><option value="voice">语音条</option></select></div><label style="display:flex;gap:6px;align-items:center;margin:0;font-size:13px;color:#475569"><input id="send" type="checkbox" checked style="width:auto"> 生成后发送</label></div>
<div class="hint">整首音频会先分离；只截取上面指定的片段做换声和混音。文件不经过微信语音条压缩，音质更好；最长 30 秒。</div></div>
<div class="panel"><div style="display:flex;justify-content:space-between;align-items:center"><strong>进度</strong><span class="badge" id="state">空闲</span></div><div class="steps" id="steps" style="margin-top:12px"><div class="badge">还没有任务。</div></div><div class="err" id="error"></div><button class="folder-btn" data-folder="jobs">打开任务输出文件夹</button></div>
<script>
let timer=null;
const folders={music:'C:\\Users\\User\\Music',models:'E:\\RVC\\app\\RVC20260718Nvidia\\assets\\weights',refs:'F:\\音频参考',jobs:'E:\\DSH\\AmoData\\songs'};
async function loadSongs(){const r=await fetch('/singing-api/library');const j=await r.json();const s=document.getElementById('song');s.innerHTML=(j.songs||[]).map(x=>`<option value="${x.path.replace(/"/g,'&quot;')}">${x.name}</option>`).join('');}
async function loadModels(){try{const r=await fetch('/singing-api/models');const j=await r.json();const s=document.getElementById('model');s.innerHTML=(j.models||[]).map(x=>`<option value="${x.path.replace(/"/g,'&quot;')}">${x.name}</option>`).join('');}catch(e){}}
async function openFolder(path){try{const r=await fetch('/singing-api/open-folder?path='+encodeURIComponent(path));const j=await r.json();if(!r.ok)throw new Error(j.error||'打开失败');}catch(e){alert(e.message);}}
function esc(v){return String(v??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function render(j){const steps=document.getElementById('steps');const state=document.getElementById('state');const err=document.getElementById('error');
 if(!j||!j.steps){steps.innerHTML='<div class="badge">还没有任务。</div>';state.textContent='空闲';err.textContent='';return;}
 state.textContent=j.status==='done'?'完成':j.status==='failed'?'失败':j.status==='running'?'进行中':'空闲';
 steps.innerHTML=j.steps.map((s,i)=>`<div class="step ${s.status}"><div class="n">${i+1}</div><div><div class="title">${esc(s.title)}</div><div class="detail">${esc(s.detail||'')}</div></div>${s.url?`<a href="${s.url}" target="_blank" rel="noopener">打开文件</a>`:''}</div>`).join('');
 err.textContent=j.error||''; if(j.status==='done'||j.status==='failed'){clearInterval(timer);timer=null;document.getElementById('run').disabled=false;}
}
async function poll(id){try{const r=await fetch('/singing-api/status?id='+encodeURIComponent(id));render(await r.json());}catch(e){}}
document.querySelectorAll('[data-folder]').forEach(b=>b.addEventListener('click',()=>openFolder(folders[b.dataset.folder])));
document.getElementById('run').addEventListener('click',async()=>{const btn=document.getElementById('run');btn.disabled=true;document.getElementById('error').textContent='';
 const body={source:document.getElementById('song').value,query:document.getElementById('song').selectedOptions[0]?.text||'',model:document.getElementById('model').value,start:Number(document.getElementById('start').value),duration:Number(document.getElementById('duration').value),send:document.getElementById('send').checked,send_mode:document.getElementById('sendMode').value};
 try{const r=await fetch('/singing-api/run',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});const j=await r.json();if(!r.ok)throw new Error(j.error||'启动失败');if(timer)clearInterval(timer);timer=setInterval(()=>poll(j.id),2000);poll(j.id);}catch(e){document.getElementById('error').textContent=e.message;btn.disabled=false;}
});
loadSongs();loadModels();
</script></div></body></html>"""


def main() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"singing service on http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
