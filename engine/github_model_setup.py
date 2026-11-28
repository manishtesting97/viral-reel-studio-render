"""Install pinned recognition models in private runner scratch before isolated use."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from huggingface_hub import snapshot_download

models_dir = Path(sys.argv[1])
mode = sys.argv[2]
if mode not in {"base", "apex"}:
    raise ValueError("Invalid recognition runtime mode.")
if mode == "base":
    lock = json.loads(Path("installers/runtime.lock.json").read_text(encoding="utf-8"))
    for name, revision in lock["models"].items():
        path = Path(snapshot_download(
            "Systran/faster-whisper-" + name,
            revision=revision,
            cache_dir=str(models_dir),
            allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.json", "vocabulary.txt"],
        ))
        ref = path.parent.parent / "refs" / "main"
        ref.parent.mkdir(exist_ok=True)
        tmp = ref.with_suffix(".tmp")
        tmp.write_text(path.name, encoding="utf-8")
        tmp.replace(ref)
        for required in ("model.bin", "config.json", "tokenizer.json"):
            assert (path / required).is_file() and (path / required).stat().st_size > 0
else:
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--no-cache-dir",
        "--index-url", "https://download.pytorch.org/whl/cpu",
        "torch==2.6.0+cpu", "torchaudio==2.6.0+cpu",
    ], check=True)
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--no-cache-dir",
        "transformers==5.24.1", "safetensors==0.8.0",
    ], check=True)
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--no-cache-dir",
        "--no-deps", "stable-ts==2.19.1",
    ], check=True)
    hf_dir = Path(os.environ["RUNNER_TEMP"]) / "vrs-apex-hf"
    snapshot_download(
        "Oriserve/Whisper-Hindi2Hinglish-Apex",
        revision="f3214eed20b4e4d4144e739982d911f87b9cb223",
        local_dir=str(hf_dir),
    )
    cfg_path = hf_dir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["dtype"] = "float32"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    gen_path = hf_dir / "generation_config.json"
    gen = json.loads(gen_path.read_text(encoding="utf-8"))
    gen["alignment_heads"] = [[2, 4], [2, 11], [3, 3], [3, 6], [3, 11], [3, 14]]
    gen_path.write_text(json.dumps(gen, indent=2), encoding="utf-8")
    out_dir = models_dir / "apex-ct2-int8"
    from ctranslate2.converters import TransformersConverter
    TransformersConverter(
        str(hf_dir),
        copy_files=["tokenizer.json", "preprocessor_config.json"],
    ).convert(str(out_dir), quantization="int8", force=True)
    ct2_cfg_path = out_dir / "config.json"
    ct2_cfg = json.loads(ct2_cfg_path.read_text(encoding="utf-8"))
    ct2_cfg["alignment_heads"] = [[2, 4], [2, 11], [3, 3], [3, 6], [3, 11], [3, 14]]
    ct2_cfg_path.write_text(json.dumps(ct2_cfg, indent=2), encoding="utf-8")
    shutil.rmtree(hf_dir, ignore_errors=True)
    for required in ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json"):
        assert (out_dir / required).is_file() and (out_dir / required).stat().st_size > 0
