#!/usr/bin/env python3
"""Run RVC singing conversion with the saved defaults."""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "rvc-config.json").read_text(encoding="utf-8"))
ROOT = Path(CONFIG["rvc_root"])
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ["weight_root"] = str(ROOT / "assets" / "weights")
os.environ["rmvpe_root"] = str(ROOT / "assets" / "rmvpe")

def detect_gender(path: str) -> tuple[str, float]:
    y, sr = librosa.load(path, sr=24000, mono=True)
    f0 = librosa.pyin(y, fmin=60, fmax=500, sr=sr, frame_length=2048, hop_length=512)[0]
    f0 = f0[~np.isnan(f0)]
    if len(f0) == 0:
        return "female", 0.0
    median = float(np.median(f0))
    return ("male" if median < 165.0 else "female"), median


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--gender", choices=["auto", "male", "female"], default="auto")
    parser.add_argument("--output", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--speaker", type=int, default=None)
    parser.add_argument("--protect", type=float, default=None)
    parser.add_argument("--rms-mix", type=float, default=None)
    parser.add_argument("--f0-method", default="")
    parser.add_argument("--peak", type=float, default=None)
    args = parser.parse_args()
    sys.argv = [sys.argv[0]]

    from configs.config import Config
    from infer.vc.modules import VC

    source = str(Path(args.source).resolve())
    gender = args.gender
    median = None
    if gender == "auto":
        gender, median = detect_gender(source)

    pitch = int(CONFIG["pitch_male"] if gender == "male" else CONFIG["pitch_female"])
    model = args.model or CONFIG["model"]
    # The UI returns an absolute .pth path, while RVC prepends its weight root.
    if Path(model).is_absolute() or "/" in model or "\\" in model:
        model = Path(model).name
    speaker = int(args.speaker if args.speaker is not None else CONFIG["speaker_id"])
    out = Path(args.output) if args.output else Path(CONFIG["output_dir"]) / f"{datetime.now():%Y%m%d-%H%M%S}-{gender}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)

    config = Config()
    config.device = "cuda"
    config.is_half = True
    vc = VC(config)
    vc.get_vc(model)
    info, audio = vc.vc_single(
        speaker,
        source,
        pitch,
        args.f0_method or CONFIG["f0_method"],
        CONFIG.get("index_path", ""),
        float(CONFIG["index_rate"]),
        int(CONFIG["resample_sr"]),
        float(args.rms_mix if args.rms_mix is not None else CONFIG["rms_mix_rate"]),
        float(args.protect if args.protect is not None else CONFIG["protect"]),
    )
    sr, data = audio
    if sr is None or data is None:
        print(json.dumps({"ok": False, "error": str(info)}, ensure_ascii=False))
        return 1
    sf.write(str(out), data, sr)
    # RVC 输出常接近 0 dBFS，先留 3 dB 余量，避免混音/编码时削顶。
    audio, sample_rate = sf.read(str(out), always_2d=False)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    target_peak = max(0.1, min(0.95, float(args.peak if args.peak is not None else CONFIG.get("peak", 0.5))))
    if peak > target_peak:
        audio = audio * (target_peak / peak)
        sf.write(str(out), audio, sample_rate)
    print(json.dumps({
        "ok": True,
        "model": model,
        "speaker_id": speaker,
        "gender": gender,
        "median_f0": round(median, 1) if median is not None else None,
        "pitch": pitch,
        "f0_method": args.f0_method or CONFIG["f0_method"],
        "protect": float(args.protect if args.protect is not None else CONFIG["protect"]),
        "rms_mix": float(args.rms_mix if args.rms_mix is not None else CONFIG["rms_mix_rate"]),
        "peak": target_peak,
        "sample_rate": sr,
        "output": str(out),
        "info": str(info),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
