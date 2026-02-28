FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VPG_DATA_DIR=/data \
    VPG_REPO_ROOT=/app \
    NLTK_DATA=/app/nltk_data \
    BLENDER_VERSION=4.1.1 \
    BLENDER_DIR=/usr/local/blender

WORKDIR /app

# System deps (ffmpeg, GL/X libs needed by Blender)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl wget xz-utils ffmpeg \
    libglu1-mesa libgl1 libx11-6 libxi6 libxxf86vm1 libxrender1 libxfixes3 libsm6 libxext6 libglib2.0-0 \
    libglvnd0 libglx0 libegl1 libgles2 libdrm2 libgbm1 \
    libxrandr2 libxkbcommon0 libwayland-client0 libwayland-server0 \
    xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

# Install Blender (Linux x64) headless (use official CDN with retries)
RUN mkdir -p ${BLENDER_DIR} /app/nltk_data && \
    BLENDER_URL="https://download.blender.org/release/Blender${BLENDER_VERSION%.*}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" && \
    curl -fL --retry 5 --retry-delay 5 "$BLENDER_URL" -o /tmp/blender.txz && \
    tar -xJf /tmp/blender.txz -C ${BLENDER_DIR} --strip-components=1 && \
    ln -sf ${BLENDER_DIR}/blender /usr/local/bin/blender && \
    rm -f /tmp/blender.txz

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
# Include pipeline code and assets in worker image
COPY scripts scripts
COPY assets assets
COPY scenes scenes
COPY manifests manifests
COPY run_full_video_creation_sequence.config.json run_full_video_creation_sequence.config.json

# Pipeline Python deps
# NOTE: `whisperx` pulls in a very large dependency tree and (by default) may try to install CUDA-enabled
# PyTorch wheels from PyPI (huge, includes nvidia-* libs). For EC2 rendering you typically don't need it.
# Enable WhisperX only when you truly need to regenerate visemes on the server:
#   docker buildx build ... --build-arg INSTALL_WHISPERX=1
ARG INSTALL_WHISPERX=0
RUN pip install --no-cache-dir g2p_en nltk requests boto3 && \
    if [ "$INSTALL_WHISPERX" = "1" ]; then \
      pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
      pip install --no-cache-dir whisperx ; \
    fi

# Preload NLTK data
RUN python -c "import nltk; nltk.download('averaged_perceptron_tagger_eng'); nltk.download('punkt')"

EXPOSE 8000
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "app.server:app"]

ENV NLTK_DATA=/app/nltk_data
RUN python -c "import nltk; nltk.download('averaged_perceptron_tagger_eng'); nltk.download('punkt')"
