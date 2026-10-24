#!/usr/bin/env python3
"""Lightweight vocal / background-music detector for the singing pipeline.

This is a reference meter, not a source separator:
- voiced activity from pYIN
- harmonic vs percussive energy
- vocal-band (300-3400 Hz) energy ratio
- spectral flatness (tonal vs noisy)

Usage:
  python vocal_check.py "audio.wav"
"""

from __future__ import annotations

import argparse
import json
import sys

import librosa
import numpy as np


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def analyze_audio(path: str, seconds: float = 30.0) -> dict:
    y, sr = librosa.load(path, sr=24000, mono=True, duration=seconds)
    if len(y) < sr // 2:
        return {"ok": False, "error": "audio too short"}
    peak = float(np.max(np.abs(y)))
    if peak > 0:
        y = y / peak * 0.95

    rms_frames = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    active_ratio = float(np.mean(rms_frames > 0.01))
    harmony, percussive = librosa.effects.hpss(y)
    total = float(np.sum(y * y)) + 1e-9
    harmonic_ratio = float(np.sum(harmony * harmony) / total)
    percussive_ratio = float(np.sum(percussive * percussive) / total)

    spectrum = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    band = spectrum[(freqs >= 300) & (freqs <= 3400)]
    band_ratio = float(np.sum(band) / (np.sum(spectrum) + 1e-9))
    flatness = float(np.mean(librosa.feature.spectral_flatness(y=y)))

    f0 = librosa.pyin(y, fmin=60, fmax=900, sr=sr, frame_length=2048, hop_length=512)[0]
    voiced_ratio = float(np.mean(~np.isnan(f0))) if len(f0) else 0.0
    median_f0 = float(np.nanmedian(f0)) if np.any(~np.isnan(f0)) else 0.0

    vocal_score = _clamp(voiced_ratio * 0.65 + harmonic_ratio * 0.35)
    music_score = _clamp(percussive_ratio * 0.55 + flatness * 4.0 + (1.0 - harmonic_ratio) * 0.20)
    rms = float(np.sqrt(np.mean(y * y)))

    return {
        "ok": True,
        "path": path,
        "seconds": round(len(y) / sr, 2),
        "active_ratio": round(active_ratio, 3),
        "voiced_ratio": round(voiced_ratio, 3),
        "rms": round(rms, 3),
        "median_f0": round(median_f0, 1),
        "harmonic_ratio": round(harmonic_ratio, 3),
        "percussive_ratio": round(percussive_ratio, 3),
        "vocal_band_ratio": round(band_ratio, 3),
        "spectral_flatness": round(flatness, 4),
        "vocal_score": round(vocal_score, 3),
        "music_score": round(music_score, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("--seconds", type=float, default=30.0)
    args = parser.parse_args()
    try:
        result = analyze_audio(args.audio, args.seconds)
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
