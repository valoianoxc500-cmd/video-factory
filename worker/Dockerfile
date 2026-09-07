# Video Factory worker: Python pipeline + FFmpeg + Node/Remotion + Chromium.
#
# Build from the REPOSITORY ROOT so the pipeline source is in context:
#   docker build -f worker/Dockerfile -t video-factory-worker .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    DEBIAN_FRONTEND=noninteractive \
    NODE_MAJOR=20

# System dependencies:
#  - ffmpeg      : render, mix and probe media
#  - fonts-noto* : Arabic glyphs for thumbnail headlines and Remotion captions
#  - libgl/libglib: OpenCV runtime
#  - the rest    : headless Chromium's shared libraries for Remotion
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg \
      ffmpeg \
      fonts-noto-core fonts-noto-cjk fonts-liberation \
      fonts-noto-color-emoji \
      libgl1 libglib2.0-0 \
      libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
      libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
      libpango-1.0-0 libcairo2 \
  && mkdir -p /etc/apt/keyrings \
  && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
  && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_$NODE_MAJOR.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
  && apt-get update && apt-get install -y --no-install-recommends nodejs \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Arabic Naskh is in a separate package on some mirrors; ignore if unavailable.
RUN apt-get update \
  && (apt-get install -y --no-install-recommends fonts-noto-naskh-arabic || true) \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY rendering/remotion/package.json rendering/remotion/package-lock.json ./rendering/remotion/
RUN cd rendering/remotion && npm ci

COPY . .

# Pre-download the Chromium build Remotion renders with, so the first job does
# not pay for it (and does not fail if egress is restricted at run time).
RUN cd rendering/remotion \
  && node node_modules/@remotion/cli/remotion-cli.js browser ensure || true

# Bundled music/SFX are gitignored; generate the synthesized defaults so a
# fresh image still produces a complete mix.
RUN python tools/make_default_audio_assets.py || true

# Cloud Run gives each instance its own disk; keep only the newest run.
ENV WORKSPACE_RETENTION=1

CMD ["python", "worker/run_worker.py"]
