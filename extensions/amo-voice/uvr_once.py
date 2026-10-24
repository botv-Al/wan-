#!/usr/bin/env python3
"""One-shot UVR vocal/instrumental separation for the singing pipeline."""

import os
import sys

ROOT = r"E:\RVC\app\RVC20260718Nvidia"
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.argv = [sys.argv[0]]

from tools.uvr5.webui import uvr  # noqa: E402

inp = os.environ["UVR_INPUT"]
out_vocal = os.environ["UVR_VOCAL"]
out_inst = os.environ["UVR_INSTRUMENTAL"]
model = os.environ.get("UVR_MODEL", "HP2_all_vocals")
for message in uvr(model, os.path.dirname(inp), out_vocal, [], out_inst, 10, "wav"):
    print(message, flush=True)
