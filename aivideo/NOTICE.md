# Third-party attribution — AI Video Maker

## MoneyPrinterTurbo

- Source: https://github.com/harry0703/MoneyPrinterTurbo
- Licence: MIT
- Copyright (c) 2024 harry0703

MoneyPrinterTurbo is MIT licensed, which permits use, modification and
distribution in a commercial product provided the copyright notice and licence
text travel with the derived work. This file is that notice.

### What was adapted

| Our module | Adapted from | What was taken |
|---|---|---|
| `aivideo/script.py` | `app/services/llm.py` | The two-part ask — narration paragraph plus short **English** search terms whatever the narration language, because stock libraries index in English. Also the "no markdown, no speaker labels, no stage directions" framing that keeps a script speakable. |
| `aivideo/footage.py` | `app/services/material.py` | The provider set (Pexels + Pixabay video search), the orientation parameter, the "pick the smallest file that still clears a resolution floor" selection, and content-hash dedup across beats. |
| `aivideo/voice.py` | `app/services/voice.py` | The key idea: Edge TTS emits `WordBoundary` events alongside the audio stream, so per-word caption timings come free with the narration — no Whisper pass and no forced aligner. Also the signed-percentage rate/volume encoding Edge expects. |
| `aivideo/subtitles.py` | `app/services/subtitle.py` | Grouping word timings into caption lines by word count and sentence punctuation. |
| `aivideo/compose.py` | `app/services/video.py` | The composition order — normalise each clip to the output canvas, concatenate, then mux narration, music and burned captions. |

### What was written from scratch

- **Checkpointed, resumable pipeline** (`aivideo/pipeline.py`). MPT runs a job
  start to finish in one process; this product must survive a worker restart
  without re-paying for a script, narration or footage that already succeeded.
- **Graded failure handling.** MPT fails a task when a stage fails. Here music
  is optional, captions degrade to estimated timings, individual clips are
  dropped and replaced, and only four conditions are genuinely terminal.
- **Arabic caption correctness** (`aivideo/subtitles.py`). MPT does not shape
  or reorder Arabic. Ours reshapes with `arabic_reshaper`, applies the bidi
  algorithm with `python-bidi`, and forces an Arabic-capable font file,
  because libass runs neither.
- **FFmpeg-direct composition.** MPT uses MoviePy. This repository already
  renders every other product through FFmpeg and MoviePy's per-frame Python
  loop is the slowest part of MPT on a long video.
- **The caption-style system** (`aivideo/spec.py`) and its ASS generation —
  presets, per-field overrides, and colour conversion to ASS's alpha-inverted
  BGR.
- **The entire customer experience.** None of MPT's Streamlit WebUI is used.

### What was deliberately not taken

- The Streamlit `webui/` application.
- MPT's paid provider integrations (Azure, MiniMax, Fish Audio, Seedance and
  the other AI-video services). Only free/keyless Edge TTS and the two free
  footage libraries this deployment already has keys for are wired up.
- MPT's Whisper subtitle path, which needs a model download and GPU time that
  Edge's word boundaries make unnecessary.
