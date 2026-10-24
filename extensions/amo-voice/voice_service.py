#!/usr/bin/env python3
"""Amo voice service: text -> GPT-SoVITS -> ToDesk virtual audio -> WeChat voice bubble.

Run with the GPT-SoVITS runtime Python:
  D:\\gpt\\GPT-SoVITS-v2pro-20250604\\GPT-SoVITS-v2pro-20250604\\runtime\\python.exe voice_service.py
"""

from __future__ import annotations

import ctypes
import json
import os
import random
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
import win32api
import win32con
import win32gui
import win32process

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
OUTPUT_DIR = Path(CONFIG["output_dir"])
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
GPT_ROOT = Path(r"E:\GPT So\GPT-SoVITS-v2pro-20250604\GPT-SoVITS-v2pro-20250604")
REF_ROOTS = [Path(r"E:\DSH\AmoData\voice-ref"), Path(r"F:\音频参考\辉夜姬中文\zn")]
LOCK = threading.Lock()
VOICEMEETER_EXE = Path(r"C:\Program Files (x86)\VB\Voicemeeter\voicemeeterpro.exe")


def ensure_virtual_mixer() -> None:
    keyword = str(CONFIG.get("virtual_speaker_keyword") or CONFIG.get("virtual_mic_keyword") or "")
    if "Voicemeeter" not in keyword or not VOICEMEETER_EXE.exists():
        return
    try:
        listing = subprocess.run(["tasklist", "/FI", "IMAGENAME eq voicemeeterpro.exe"],
                                 capture_output=True, text=True, timeout=5).stdout.lower()
        if "voicemeeterpro.exe" in listing:
            return
        log("starting Voicemeeter Banana")
        subprocess.Popen([str(VOICEMEETER_EXE)], creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(4)
    except Exception as error:
        log(f"ensure Voicemeeter failed: {error}")


VK_RMENU = 0xA5
SCAN_RMENU = 0x38
EXTENDED = 0x0001
KEYUP = 0x0002


def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    print(line, flush=True)
    with (BASE / "voice_service.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


EMOTION_ROOT = Path(CONFIG.get("emotion_ref_root", r"F:\音频参考\辉夜姬中文"))
EMOTION_FOLDERS = {
    "happy": "开心",
    "sad": "伤心",
    "confused": "疑惑",
    "affirm": "肯定",
    "daily": "日常",
}
EMOTION_WORDS = {
    "happy": ("开心", "高兴", "喜欢", "太好了", "好耶", "哈哈", "嘿嘿", "笑", "可爱", "棒", "爱你"),
    "sad": ("难过", "伤心", "累", "哭", "委屈", "孤独", "烦", "不开心", "低落", "不想"),
    "confused": ("为什么", "怎么会", "真的假的", "疑惑", "奇怪", "不懂", "啊？", "吗？", "？"),
    "affirm": ("当然", "没问题", "确定", "可以", "好的", "行", "对的", "嗯", "一定"),
}


def detect_emotion(text: str) -> str:
    body = str(text or "")
    scores = {name: sum(body.count(word) for word in words) for name, words in EMOTION_WORDS.items()}
    best = max(scores, key=scores.get)
    # Casual conversation dominates; only switch when there is a clear cue.
    if scores[best] >= 2 or (best == "confused" and "？" in body):
        return best
    return "daily"


def pick_emotion_reference(emotion: str, text: str) -> dict[str, str] | None:
    key = str(emotion or "").strip().lower()
    folder = None
    if key and valid_emotion_name(key) and (EMOTION_ROOT / key).is_dir():
        folder = EMOTION_ROOT / key
    else:
        if key not in EMOTION_FOLDERS:
            key = detect_emotion(text)
        folder = EMOTION_ROOT / EMOTION_FOLDERS.get(key, "日常")
        # A custom folder such as 想你 can be picked when its name appears in the reply.
        if key == "daily" and EMOTION_ROOT.is_dir():
            for candidate in EMOTION_ROOT.iterdir():
                if candidate.is_dir() and valid_emotion_name(candidate.name) and candidate.name not in EMOTION_FOLDERS.values() and candidate.name in str(text or ""):
                    folder, key = candidate, candidate.name
                    break
    if not folder or not folder.is_dir():
        return None
    wavs = sorted(path for path in folder.glob("*.wav") if path.is_file())
    if not wavs:
        return None
    wav = random.choice(wavs)
    prompt = str(CONFIG.get("prompt_text", ""))
    transcript = wav.with_suffix(".txt")
    try:
        line = transcript.read_text(encoding="utf-8-sig").strip()
        parts = line.split("|")
        if len(parts) >= 4 and parts[3].strip():
            prompt = parts[3].strip()
    except Exception:
        pass
    return {"emotion": key, "ref_audio": str(wav), "prompt_text": prompt}


def valid_emotion_name(name: str) -> bool:
    value = str(name or "").strip()
    return bool(value) and len(value) <= 40 and not value.lower().startswith("zn") and value != "_trash" and not any(ch in value for ch in '\\/:*?"<>|') and value not in {".", ".."}


def emotion_dir(name: str) -> Path:
    if not valid_emotion_name(name):
        raise ValueError("invalid emotion folder name")
    root = EMOTION_ROOT.resolve()
    path = (root / str(name).strip()).resolve()
    if path.parent != root:
        raise ValueError("emotion folder outside root")
    return path


def list_emotion_folders() -> list[dict[str, Any]]:
    root = EMOTION_ROOT
    if not root.is_dir():
        return []
    folders = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not path.is_dir() or not valid_emotion_name(path.name):
            continue
        folders.append({
            "name": path.name,
            "path": str(path),
            "count": len(list(path.glob("*.wav"))),
        })
    return folders



def find_wechat_window() -> int:
    process_name = CONFIG["wechat_process"].lower()
    found: list[tuple[int, int]] = []

    def callback(hwnd: int, _extra: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            handle = win32api.OpenProcess(0x0400 | 0x0010, False, pid)
            try:
                executable = win32process.GetModuleFileNameEx(handle, 0)
            finally:
                win32api.CloseHandle(handle)
        except Exception:
            return
        if os.path.basename(executable).lower() != process_name:
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        found.append(((right - left) * (bottom - top), hwnd))

    win32gui.EnumWindows(callback, None)
    return max(found)[1] if found else 0


def focus_wechat() -> int:
    hwnd = find_wechat_window()
    if not hwnd:
        raise RuntimeError("WECHAT_WINDOW_NOT_FOUND")
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        try:
            win32gui.BringWindowToTop(hwnd)
        except Exception:
            pass
    time.sleep(0.8)
    return hwnd


def focus_wechat_chat(name: str = '') -> int:
    hwnd = focus_wechat()
    if not name:
        return hwnd
    # WeChat global search: Ctrl+F, type the contact name, Enter.
    try:
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(0x46, 0, 0, 0)
        win32api.keybd_event(0x46, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.4)
        for char in name:
            if not char.isascii() or not char.isalnum() and char not in " -_.":
                continue
            code = ord(char.upper())
            shift = char.isupper()
            if shift:
                win32api.keybd_event(win32con.VK_SHIFT, 0, 0, 0)
            win32api.keybd_event(code, 0, 0, 0)
            win32api.keybd_event(code, 0, win32con.KEYEVENTF_KEYUP, 0)
            if shift:
                win32api.keybd_event(win32con.VK_SHIFT, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.05)
        time.sleep(0.4)
        win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
        win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(1.0)
    except Exception as error:
        log(f"focus chat failed: {error}")
    return hwnd


def _device_index(keyword: str, output: bool) -> int:
    candidates: list[tuple[int, int, str]] = []
    for index, device in enumerate(sd.query_devices()):
        name = str(device.get("name", ""))
        if keyword not in name:
            continue
        if output and device.get("max_output_channels", 0) <= 0:
            continue
        if not output and device.get("max_input_channels", 0) <= 0:
            continue
        try:
            host = sd.query_hostapis(device["hostapi"])["name"]
        except Exception:
            host = ""
        score = 2 if "WASAPI" in host else 1
        candidates.append((score, index, f"{name} / {host}"))
    if not candidates:
        raise RuntimeError(f"AUDIO_DEVICE_NOT_FOUND:{keyword}")
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    log(f"audio device selected: {candidates[0][2]}")
    return candidates[0][1]


def set_default_microphone(keyword: str) -> None:
    script = (
        "Import-Module AudioDeviceCmdlets; "
        f"$d=Get-AudioDevice -List | Where-Object {{ $_.Type -eq 'Recording' -and $_.Name -like '*{keyword}*' }} | Select-Object -First 1; "
        "if(-not $d){exit 2}; Set-AudioDevice -Index $d.Index; exit 0"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(f"MIC_SWITCH_FAILED:{keyword}:{result.returncode}")


def ensure_voice_model() -> None:
    for endpoint, key in (("set_gpt_weights", "gpt_weights"), ("set_sovits_weights", "sovits_weights")):
        path = CONFIG.get(key)
        if not path:
            continue
        url = f"http://127.0.0.1:9880/{endpoint}?weights_path={urllib.parse.quote(path)}"
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                if response.status != 200:
                    raise RuntimeError(response.status)
            log(f"{endpoint}: {path}")
        except Exception as error:
            log(f"{endpoint} failed: {error}")


def list_weights(prefix: str, ext: str) -> list[str]:
    if not GPT_ROOT.exists():
        return []
    files: list[Path] = []
    for folder in GPT_ROOT.glob(prefix + "*"):
        if folder.is_dir():
            files.extend(folder.glob("*" + ext))
    return sorted(str(p.relative_to(GPT_ROOT)).replace("\\", "/") for p in files)


def list_refs() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in [Path(CONFIG.get("ref_audio", "")), *[p for root in REF_ROOTS if root.exists() for p in root.rglob("*.wav")]]:
        if not str(path) or not path.is_file() or str(path) in seen:
            continue
        seen.add(str(path))
        rows.append({"name": path.name, "path": str(path)})
    return rows[:120]


def set_voice_weights(gpt: str | None, sovits: str | None) -> None:
    pairs = [("gpt_weights", gpt), ("sovits_weights", sovits)]
    for key, value in pairs:
        if not value:
            continue
        endpoint = f"{CONFIG['gpt_sovits_url'].rsplit('/tts', 1)[0]}/set_{key}"
        url = endpoint + "?weights_path=" + urllib.parse.quote(str(value), safe="")
        with urllib.request.urlopen(url, timeout=60) as response:
            if response.status != 200:
                raise RuntimeError(f"SET_{key.upper()}_HTTP_{response.status}")
        log(f"set_{key}: {value}")


def open_explorer(path: Path) -> None:
    target = path if path.is_dir() else path.parent
    subprocess.Popen(["explorer.exe", str(target)])


def synthesize(text: str, options: dict[str, Any] | None = None) -> Path:
    options = options or {}
    set_voice_weights(options.get("gpt_weights") if "gpt_weights" in options else None,
                      options.get("sovits_weights") if "sovits_weights" in options else None)

    def pick(key: str, default: Any) -> Any:
        return options.get(key, CONFIG.get(key, default))

    payload = {
        "text": text,
        "text_lang": pick("text_lang", "zh"),
        "ref_audio_path": pick("ref_audio", CONFIG["ref_audio"]),
        "prompt_lang": pick("prompt_lang", "zh"),
        "prompt_text": pick("prompt_text", CONFIG.get("prompt_text", "")),
        "text_split_method": "cut0",
        "batch_size": 1,
        "media_type": "wav",
        "streaming_mode": False,
        "parallel_infer": False,
        "split_bucket": False,
        "temperature": float(pick("temperature", 0.55)),
        "top_k": int(pick("top_k", 15)),
        "top_p": float(pick("top_p", 0.85)),
        "seed": int(pick("seed", 12345)),
        "repetition_penalty": float(pick("repetition_penalty", 1.35)),
        "speed_factor": float(pick("speed_factor", 1.0)),
    }
    if not options.get("ref_audio") or options.get("emotion"):
        reference = pick_emotion_reference(str(options.get("emotion", "")), text)
        if reference:
            payload["ref_audio_path"] = reference["ref_audio"]
            payload["prompt_text"] = reference["prompt_text"]
            log(f"emotion reference: {reference['emotion']} / {Path(reference['ref_audio']).name}")
    request = urllib.request.Request(
        CONFIG["gpt_sovits_url"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            content_type = response.headers.get("Content-Type", "")
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"TTS_HTTP_{error.code}:{detail}") from error
    if not content_type.startswith("audio/") or len(body) < 1024:
        raise RuntimeError(f"TTS_BAD_RESPONSE:{content_type}:{len(body)}")
    target = OUTPUT_DIR / f"{datetime.now():%Y%m%d-%H%M%S-%f}.wav"
    target.write_bytes(body)
    return target


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def process_audio(path: Path) -> Path | None:
    options = CONFIG.get("audio_process", {})
    normalize = bool(options.get("normalize", False))
    volume = float(options.get("volume", 1.0))
    loudness = options.get("loudness", "quiet")
    targets = {"quiet": -20, "normal": -16, "loud": -13}
    if not normalize and abs(volume - 1.0) < 1e-9:
        return None
    ffmpeg = CONFIG.get("ffmpeg_path") or "ffmpeg"
    out = OUTPUT_DIR / f"proc-{datetime.now():%Y%m%d-%H%M%S-%f}.wav"
    if normalize:
        target = targets.get(str(loudness), -20)
        audio_filter = f"loudnorm=I={target}:TP=-1.5:LRA=11"
        detail = f"loudnorm {target} LUFS"
    else:
        audio_filter = f"volume={volume}"
        detail = f"volume {volume}"
    if options.get("eq"):
        audio_filter += (",highpass=f=80,"
                         "equalizer=f=250:width_type=o:width=1:g=-2,"
                         "equalizer=f=3000:width_type=o:width=1:g=2.5,"
                         "equalizer=f=9000:width_type=o:width=1:g=1")
        detail += " + eq"
    if options.get("compressor"):
        audio_filter += ",acompressor=threshold=-18dB:ratio=2:attack=10:release=100:makeup=1.6"
        detail += " + compressor"
    limiter = float(options.get("limiter", 0) or 0)
    if 0 < limiter < 1:
        audio_filter += f",alimiter=limit={limiter}"
        detail += f" + limiter {limiter}"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
               "-af", audio_filter, "-ac", "1", "-ar", "48000", str(out)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        log(f"audio process failed: {detail}: {result.stderr[:200]}")
        return None
    if 0 < limiter < 1:
        with wave.open(str(out), "rb") as handle:
            rate = handle.getframerate()
            channels = handle.getnchannels()
            raw = handle.readframes(handle.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        peak = float(np.max(np.abs(samples))) / 32768.0
        if peak > limiter:
            samples *= limiter / peak
        with wave.open(str(out), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(np.clip(samples, -32768, 32767).astype(np.int16).tobytes())
        detail += " + peak cap"
    log(f"audio processed: {detail}")
    return out


def attenuate_peak(path: Path, target: float = 0.35) -> Path:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        raw = handle.readframes(handle.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    peak = float(np.max(np.abs(samples))) / 32768.0 if len(samples) else 0.0
    if peak <= target or peak == 0:
        return path
    samples *= target / peak
    out = OUTPUT_DIR / f"direct-{datetime.now():%Y%m%d-%H%M%S-%f}.wav"
    with wave.open(str(out), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(np.clip(samples, -32768, 32767).astype(np.int16).tobytes())
    log(f"direct peak attenuated: {peak:.3f} -> {target:.3f}")
    return out


def play_to_virtual_speaker(path: Path) -> None:
    ensure_virtual_mixer()
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        raw = handle.readframes(handle.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    target_rate = 48000
    if rate != target_rate:
        count = int(len(data) * target_rate / rate)
        data = np.interp(
            np.linspace(0, len(data) - 1, count), np.arange(len(data)), data
        ).astype(np.float32)
    candidates: list[tuple[int, int, str]] = []
    speaker_keyword = CONFIG.get("virtual_speaker_keyword") or CONFIG["virtual_mic_keyword"]
    for index, device in enumerate(sd.query_devices()):
        name = str(device.get("name", ""))
        if speaker_keyword not in name or device.get("max_output_channels", 0) <= 0:
            continue
        try:
            host = sd.query_hostapis(device["hostapi"])["name"]
        except Exception:
            host = ""
        score = 3 if "MME" in host else 2 if "WASAPI" in host else 1
        candidates.append((score, index, f"{name} / {host}"))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not candidates:
        raise RuntimeError("VIRTUAL_SPEAKER_NOT_FOUND")
    errors: list[str] = []
    for _score, index, label in candidates:
        try:
            sd.play(data, samplerate=target_rate, device=index)
            sd.wait()
            log(f"played to: {label}")
            return
        except Exception as error:
            errors.append(f"{label}: {error}")
    raise RuntimeError("VIRTUAL_SPEAKER_PLAY_FAILED:" + " | ".join(errors)[:300])


def send_voice(path: Path, conversation_name: str = '') -> None:
    name = conversation_name or str(CONFIG.get('chat_name', 'Amon'))
    ensure_virtual_mixer()
    hwnd = focus_wechat_chat(name)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    x = left + int((right - left) * 0.68)
    y = top + int((bottom - top) * 0.88)
    set_default_microphone(CONFIG["virtual_mic_keyword"])
    try:
        win32api.SetCursorPos((x, y))
        time.sleep(0.2)
        win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        time.sleep(0.2)
        win32api.keybd_event(VK_RMENU, SCAN_RMENU, EXTENDED, 0)
        time.sleep(0.8)
        play_to_virtual_speaker(path)
        time.sleep(0.8)
        win32api.keybd_event(VK_RMENU, SCAN_RMENU, EXTENDED | KEYUP, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        time.sleep(1.0)
    finally:
        try:
            set_default_microphone(CONFIG["real_mic_keyword"])
        except Exception as error:
            log(f"restore microphone failed: {error}")


VOICE_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Amo 语音</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#1e293b;font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:28px 20px 60px;display:flex;flex-direction:column;gap:16px}
h1{font-size:24px;margin:0}p.sub{color:#64748b;margin:0}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:18px;display:flex;flex-direction:column;gap:12px;box-shadow:0 2px 8px rgba(15,23,42,.04)}
.field{display:flex;flex-direction:column;gap:5px}label{font-size:12px;color:#64748b}
input,textarea{width:100%;padding:9px 10px;border:1px solid #cbd5e1;border-radius:8px;font:inherit;background:#fff;color:inherit}
textarea{min-height:84px;resize:vertical}.actions{display:flex;flex-direction:column;gap:8px}button{border:0;border-radius:9px;background:#0f766e;color:#fff;padding:11px 18px;font:inherit;cursor:pointer}
button.secondary{background:#e2e8f0;color:#334155}button:disabled{opacity:.5;cursor:default}.status{color:#64748b;font-size:12px;min-height:20px}
audio{width:100%}a{color:#0f766e;font-weight:600}.folders{display:flex;flex-direction:column;gap:6px}.folders button{background:#e2e8f0;color:#334155;padding:8px 10px;text-align:left}.folder-btn{align-self:flex-start;background:#e2e8f0;color:#334155;padding:6px 9px;font-size:12px;margin-top:2px;text-align:left}
.emotion-head{display:flex;justify-content:space-between;align-items:center;gap:12px}
.emotion-new{display:flex;gap:8px;margin-top:10px}.emotion-new input{flex:1}
.emotion-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:9px 10px;border:1px solid #e2e8f0;border-radius:8px}
.emotion-row strong{font-size:13px}.emotion-row small{display:block;color:#64748b;font-size:11px;margin-top:2px}
.emotion-actions{display:flex;gap:6px;flex-wrap:wrap}.emotion-actions button{padding:5px 8px;font-size:12px;background:#e2e8f0;color:#334155}
</style></head><body><div class="wrap">
<h1>语音</h1><p class="sub">直接调 GPT-SoVITS API，生成可试听；发送按钮走微信语音条。</p>
<div class="card">
<div class="emotion-head"><strong>情绪参考文件夹</strong><button id="openEmotionRoot" class="folder-btn" style="margin:0">打开参考根目录</button></div>
<p class="sub" style="margin:6px 0 0">语音会根据当前氛围选择对应文件夹，再随机取一条 wav + 同名 txt 作为参考；没有线索时默认用“日常”。</p>
<div class="emotion-new"><input id="emotionName" placeholder="新建情绪文件夹，例如：想你"><button id="createEmotion">新建</button></div>
<div id="emotionList" class="folders" style="margin-top:10px"></div>
<div class="status" id="emotionStatus"></div>
</div>
<div class="card">
<div class="field"><label>文本</label><textarea id="text">哥哥，我在呢，你先别急。</textarea></div>
<div class="field"><label>GPT 权重</label><select id="gpt_weights"></select><button class="folder-btn" data-folder="models">打开 GPT 模型文件夹</button></div>
<div class="field"><label>SoVITS 权重</label><select id="sovits_weights"></select><button class="folder-btn" data-folder="models">打开 SoVITS 模型文件夹</button></div>
<div class="field"><label>参考音频选择</label><select id="ref_choice"></select><button class="folder-btn" data-folder="refs">打开参考音频文件夹</button></div>
<div class="field"><label>参考音频路径</label><input id="ref_audio"></div>
<div class="field"><label>参考原文</label><textarea id="prompt_text"></textarea></div>
<div class="field"><label>参考语言</label><input id="prompt_lang"></div>
<div class="field"><label>temperature</label><input id="temperature" type="number" step="0.05" min="0.1" max="1.5"></div>
<div class="field"><label>top_p</label><input id="top_p" type="number" step="0.05" min="0.1" max="1"></div>
<div class="field"><label>top_k</label><input id="top_k" type="number" step="1" min="1" max="100"></div>
<div class="field"><label>speed_factor（语速）</label><input id="speed_factor" type="number" step="0.05" min="0.5" max="1.5"></div>
<div class="field"><label>seed</label><input id="seed" type="number" step="1"></div>
<div class="field"><label>直出峰值</label><input id="peak" type="number" step="0.05" min="0.1" max="0.9"></div>
<div class="actions"><button id="preview">生成试听</button><button id="send" class="secondary">生成并发送语音条</button><button id="save" class="secondary">保存为默认</button></div>
<button class="folder-btn" data-folder="output">打开语音输出文件夹</button>
<div class="status" id="status">就绪</div>
<audio id="player" controls hidden></audio>
<a id="download" href="#" target="_blank" rel="noopener" hidden>打开生成文件</a>
</div>
<script>
const $=id=>document.getElementById(id);
const keys=['ref_audio','prompt_text','prompt_lang','temperature','top_p','top_k','speed_factor','seed','peak','gpt_weights','sovits_weights'];
const folders={refs:'E:\\DSH\\AmoData\\voice-ref',models:'E:\\GPT So\\GPT-SoVITS-v2pro-20250604\\GPT-SoVITS-v2pro-20250604',output:'D:\\gpt\\voice-out'};
async function loadModels(){try{const r=await fetch('/voice-api/models');const m=await r.json();for(const k of ['gpt_weights','sovits_weights']){const s=$(k);const list=k==='gpt_weights'?m.gpt:m.sovits;s.innerHTML=(list||[]).map(x=>`<option value="${x.replace(/"/g,'&quot;')}">${x}</option>`).join('');if(m[k])s.value=m[k];}}catch(e){}}
async function loadRefs(){try{const r=await fetch('/voice-api/refs');const j=await r.json();const s=$('ref_choice');s.innerHTML=(j.refs||[]).map(x=>`<option value="${x.path.replace(/"/g,'&quot;')}">${x.name}</option>`).join('');if(j.current)s.value=j.current;}catch(e){}}
const esc=v=>String(v??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let emotionRoot='';
async function openFolder(path){try{const r=await fetch('/voice-api/open-folder?path='+encodeURIComponent(path));const j=await r.json();if(!r.ok)throw new Error(j.error||'打开失败');}catch(e){alert(e.message);}}
async function emotionAction(action,name,extra={}){try{const r=await fetch('/voice-api/emotions/'+action,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name,...extra})});const j=await r.json();if(!r.ok)throw new Error(j.error||'操作失败');$('emotionStatus').textContent='已完成';await loadEmotions();}catch(e){$('emotionStatus').textContent='错误：'+e.message;}}
async function loadEmotions(){try{const r=await fetch('/voice-api/emotions');const j=await r.json();emotionRoot=j.root||'';const list=$('emotionList');list.innerHTML=(j.folders||[]).map(f=>`<div class="emotion-row"><div><strong>${esc(f.name)}</strong><small>${f.count} 条 wav</small></div><div class="emotion-actions"><button data-open="${esc(f.path)}">打开</button><button data-rename="${esc(f.name)}">改名</button><button data-delete="${esc(f.name)}">删除</button></div></div>`).join('')||'<div class="badge">还没有情绪文件夹。</div>';}catch(e){$('emotionStatus').textContent='读取失败：'+e.message;}}
$('emotionList').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.open)openFolder(b.dataset.open);else if(b.dataset.rename){const name=prompt('新的文件夹名',b.dataset.rename);if(name&&name!==b.dataset.rename)emotionAction('rename',b.dataset.rename,{old:b.dataset.rename,new:name});}else if(b.dataset.delete){if(confirm('删除情绪文件夹“'+b.dataset.delete+'”？会先移到 _trash 回收目录。'))emotionAction('delete',b.dataset.delete);}});
$('createEmotion').onclick=()=>{const name=$('emotionName').value.trim();if(!name)return;emotionAction('create',name).then(()=>{$('emotionName').value='';});};
$('openEmotionRoot').onclick=()=>openFolder(emotionRoot);
async function load(){try{const r=await fetch('/voice-api/config');const c=await r.json();for(const k of keys)if(c[k]!==undefined&&$(k).tagName!=='SELECT')$(k).value=c[k];}catch(e){$('status').textContent='配置读取失败: '+e.message;}await loadModels();await loadRefs();await loadEmotions();}
$('ref_choice').onchange=()=>{$('ref_audio').value=$('ref_choice').value;};
document.querySelectorAll('[data-folder]').forEach(b=>b.addEventListener('click',async()=>{try{const r=await fetch('/voice-api/open-folder?path='+encodeURIComponent(folders[b.dataset.folder]));const j=await r.json();if(!r.ok)throw new Error(j.error||'打开失败');}catch(e){alert(e.message);}}));
async function run(send){const body={text:$('text').value};for(const k of keys)body[k]=$(k).value;body.send=send;
 $('status').textContent=send?'生成并发送中…':'生成中…';for(const b of ['preview','send'])$(b).disabled=true;
 try{const r=await fetch('/voice-api/'+(send?'send':'generate'),{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});const j=await r.json();if(!r.ok)throw new Error(j.error||'失败');
 $('status').textContent=(send?'已发送：':'已生成：')+j.duration+'s';$('player').src=j.url+'&t='+Date.now();$('player').hidden=false;$('player').play().catch(()=>{});$('download').href=j.url;$('download').hidden=false;
 }catch(e){$('status').textContent='错误：'+e.message;}finally{for(const b of ['preview','send'])$(b).disabled=false;}
}
$('preview').onclick=()=>run(false);$('send').onclick=()=>run(true);
$('save').onclick=async()=>{const body={};for(const k of keys)body[k]=$(k).value;try{const r=await fetch('/voice-api/save',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});const j=await r.json();if(!r.ok)throw new Error(j.error||'保存失败');$('status').textContent='已保存为默认';}catch(e){$('status').textContent='保存失败：'+e.message;}};
load();
</script></div></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "AmoVoice/0.1"

    def _json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html_page(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_audio(self, raw: str) -> None:
        path = Path(raw).resolve()
        if path != OUTPUT_DIR.resolve() and OUTPUT_DIR.resolve() not in path.parents:
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not path.is_file():
            self._json(404, {"ok": False, "error": "not found"})
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/voice", "/voice/"}:
            self._html_page(VOICE_HTML)
            return
        if parsed.path == "/voice-api/models":
            self._json(200, {
                "gpt": list_weights("GPT_weights", ".ckpt"),
                "sovits": list_weights("SoVITS_weights", ".pth"),
                "gpt_weights": CONFIG.get("gpt_weights", ""),
                "sovits_weights": CONFIG.get("sovits_weights", ""),
            })
            return
        if parsed.path == "/voice-api/refs":
            self._json(200, {"refs": list_refs(), "current": CONFIG.get("ref_audio", "")})
            return
        if parsed.path == "/voice-api/emotions":
            self._json(200, {"root": str(EMOTION_ROOT), "folders": list_emotion_folders()})
            return
        if parsed.path == "/voice-api/open-folder":
            query = urllib.parse.parse_qs(parsed.query)
            path = Path((query.get("path") or [""])[0]).resolve()
            allowed = [OUTPUT_DIR.resolve(), GPT_ROOT.resolve(), EMOTION_ROOT.resolve(), *[r.resolve() for r in REF_ROOTS]]
            if not any(path == root or root in path.parents for root in allowed) or not path.exists():
                self._json(404, {"ok": False, "error": "path not allowed"})
                return
            open_explorer(path)
            self._json(200, {"ok": True})
            return
        if parsed.path == "/voice-api/config":
            self._json(200, {
                "ref_audio": CONFIG.get("ref_audio", ""),
                "prompt_text": CONFIG.get("prompt_text", ""),
                "prompt_lang": CONFIG.get("prompt_lang", "zh"),
                "temperature": CONFIG.get("temperature", 0.55),
                "top_p": CONFIG.get("top_p", 0.85),
                "top_k": CONFIG.get("top_k", 15),
                "seed": CONFIG.get("seed", 12345),
                "direct_peak": CONFIG.get("direct_peak", 0.35),
                "speed_factor": CONFIG.get("speed_factor", 1.0),
                "gpt_weights": CONFIG.get("gpt_weights", ""),
                "sovits_weights": CONFIG.get("sovits_weights", ""),
            })
            return
        if parsed.path == "/voice-api/file":
            query = urllib.parse.parse_qs(parsed.query)
            self._serve_audio((query.get("path") or [""])[0])
            return
        if parsed.path != "/health":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            _device_index(CONFIG.get("virtual_speaker_keyword") or CONFIG["virtual_mic_keyword"], output=True)
            hwnd = find_wechat_window()
            self._json(200, {"ok": True, "wechat": bool(hwnd), "gpt_sovits": CONFIG["gpt_sovits_url"]})
        except Exception as error:
            self._json(500, {"ok": False, "error": str(error)})

    def _voice_save(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid json"})
            return
        allowed = {"ref_audio", "prompt_text", "prompt_lang", "text_lang", "temperature", "top_p", "top_k", "seed", "direct_peak", "speed_factor", "gpt_weights", "sovits_weights"}
        for key, value in body.items():
            if key in allowed:
                CONFIG[key] = value
        (BASE / "config.json").write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log("voice defaults saved")
        self._json(200, {"ok": True})

    def _voice_generate(self, send: bool) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid json"})
            return
        text = str(body.get("text", "")).strip()
        if not text:
            self._json(400, {"ok": False, "error": "text is required"})
            return
        options = {k: body[k] for k in ("ref_audio", "prompt_text", "prompt_lang", "text_lang", "temperature", "top_p", "top_k", "seed", "repetition_penalty", "speed_factor", "gpt_weights", "sovits_weights") if k in body}
        conversation_name = str(body.get("conversation_name", "")).strip()
        if not LOCK.acquire(blocking=False):
            self._json(409, {"ok": False, "error": "busy"})
            return
        try:
            started = time.time()
            path = synthesize(text, options)
            peak = float(body.get("peak", CONFIG.get("direct_peak", 0.35)))
            path = attenuate_peak(path, peak)
            duration = wav_duration(path)
            if duration > float(CONFIG["max_seconds"]):
                raise ValueError(f"audio too long: {duration:.1f}s")
            if send:
                send_voice(path, conversation_name)
            url = "/voice-api/file?path=" + urllib.parse.quote(str(path), safe="")
            log(f"voice {'send' if send else 'preview'} ok duration={duration:.2f}s path={path}")
            self._json(200, {"ok": True, "duration": round(duration, 2), "path": str(path), "url": url, "sent": send, "elapsed": round(time.time() - started, 2)})
        except Exception as error:
            log(f"voice generate failed: {error}")
            self._json(500, {"ok": False, "error": str(error)})
        finally:
            LOCK.release()

    def _send_audio(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            path = Path(str(body.get("path", ""))).resolve()
            allowed = [OUTPUT_DIR.resolve(), Path("E:/DSH/AmoData/voice-out").resolve(), Path("E:/DSH/AmoData/songs").resolve()]
            if not any(path == root or root in path.parents for root in allowed):
                raise ValueError("path outside voice output")
            if not path.is_file():
                raise FileNotFoundError(str(path))
            dry_run = bool(body.get("dry_run"))
            conversation_name = str(body.get("conversation_name", "")).strip()
        except Exception as error:
            self._json(400, {"ok": False, "error": f"bad audio request: {error}"})
            return
        if not LOCK.acquire(blocking=False):
            self._json(409, {"ok": False, "error": "busy"})
            return
        try:
            started = time.time()
            processed = process_audio(path)
            if processed is not None:
                path = processed
            duration = wav_duration(path)
            if duration > float(CONFIG["max_seconds"]):
                raise ValueError(f"audio too long: {duration:.1f}s")
            if not dry_run:
                send_voice(path, conversation_name)
            log(f"send_audio ok dry={dry_run} duration={duration:.2f}s path={path}")
            self._json(200, {"ok": True, "duration": round(duration, 2), "path": str(path), "dryRun": dry_run, "elapsed": round(time.time() - started, 2)})
        except Exception as error:
            log(f"send_audio failed: {error}")
            self._json(500, {"ok": False, "error": str(error)})
        finally:
            LOCK.release()

    def _say_direct(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid json"})
            return
        text = str(body.get("text", "")).strip()
        if not text:
            self._json(400, {"ok": False, "error": "text is required"})
            return
        if len(text) > int(CONFIG["max_chars"]):
            self._json(400, {"ok": False, "error": "text too long"})
            return
        dry_run = bool(body.get("dry_run"))
        conversation_name = str(body.get("conversation_name", "")).strip()
        if not LOCK.acquire(blocking=False):
            self._json(409, {"ok": False, "error": "busy"})
            return
        try:
            started = time.time()
            options = {"emotion": str(body.get("emotion", "")).strip()} if body.get("emotion") else {}
            path = synthesize(text, options)
            path = attenuate_peak(path, float(CONFIG.get("direct_peak", 0.35)))
            duration = wav_duration(path)
            if duration > float(CONFIG["max_seconds"]):
                path.unlink(missing_ok=True)
                self._json(400, {"ok": False, "error": "audio too long"})
                return
            if not dry_run:
                send_voice(path, conversation_name)
            log(f"say_direct ok dry={dry_run} duration={duration:.2f}s path={path}")
            self._json(200, {"ok": True, "duration": round(duration, 2), "path": str(path), "dryRun": dry_run, "elapsed": round(time.time() - started, 2)})
        except Exception as error:
            log(f"say_direct failed: {error}")
            self._json(500, {"ok": False, "error": str(error)})
        finally:
            LOCK.release()

    def _voice_emotions(self, action: str) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid json"})
            return
        try:
            EMOTION_ROOT.mkdir(parents=True, exist_ok=True)
            name = str(body.get("name", "")).strip()
            if action == "create":
                path = emotion_dir(name)
                if path.exists():
                    raise ValueError("文件夹已存在")
                path.mkdir()
            elif action == "rename":
                old = str(body.get("old", "")).strip()
                new = str(body.get("new", "")).strip()
                source = emotion_dir(old)
                target = emotion_dir(new)
                if not source.is_dir():
                    raise ValueError("原文件夹不存在")
                if target.exists():
                    raise ValueError("新名称已存在")
                source.rename(target)
            elif action == "delete":
                source = emotion_dir(name)
                if not source.is_dir():
                    raise ValueError("文件夹不存在")
                trash = EMOTION_ROOT / "_trash"
                trash.mkdir(exist_ok=True)
                target = trash / (source.name + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
                shutil.move(str(source), str(target))
            else:
                raise ValueError("unknown action")
            self._json(200, {"ok": True, "folders": list_emotion_folders()})
        except Exception as error:
            self._json(400, {"ok": False, "error": str(error)})

    def do_POST(self) -> None:
        if self.path in {"/voice-api/emotions/create", "/voice-api/emotions/rename", "/voice-api/emotions/delete"}:
            self._voice_emotions(self.path.rsplit("/", 1)[-1])
            return
        if self.path == "/voice-api/save":
            self._voice_save()
            return
        if self.path == "/voice-api/generate":
            self._voice_generate(False)
            return
        if self.path == "/voice-api/send":
            self._voice_generate(True)
            return
        if self.path == "/send_audio":
            self._send_audio()
            return
        if self.path == "/say_direct":
            self._say_direct()
            return
        if self.path != "/say":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid json"})
            return
        text = str(body.get("text", "")).strip()
        if not text:
            self._json(400, {"ok": False, "error": "text is required"})
            return
        if len(text) > int(CONFIG["max_chars"]):
            self._json(400, {"ok": False, "error": "text too long"})
            return
        dry_run = bool(body.get("dry_run"))
        conversation_name = str(body.get("conversation_name", "")).strip()
        if not LOCK.acquire(blocking=False):
            self._json(409, {"ok": False, "error": "busy"})
            return
        try:
            started = time.time()
            options = {"emotion": str(body.get("emotion", "")).strip()} if body.get("emotion") else {}
            path = synthesize(text, options)
            processed = process_audio(path)
            if processed is not None:
                path = processed
            duration = wav_duration(path)
            if duration > float(CONFIG["max_seconds"]):
                path.unlink(missing_ok=True)
                self._json(400, {"ok": False, "error": "audio too long"})
                return
            if not dry_run:
                send_voice(path, conversation_name)
            log(f"say ok dry={dry_run} duration={duration:.2f}s path={path}")
            self._json(200, {"ok": True, "duration": round(duration, 2), "path": str(path), "dryRun": dry_run, "elapsed": round(time.time() - started, 2)})
        except Exception as error:
            log(f"say failed: {error}")
            self._json(500, {"ok": False, "error": str(error)})
        finally:
            LOCK.release()

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    ensure_voice_model()
    server = ThreadingHTTPServer((CONFIG["host"], int(CONFIG["port"])), Handler)
    log(f"listening http://{CONFIG['host']}:{CONFIG['port']}")
    server.serve_forever()


if __name__ == "__main__":
    main()
