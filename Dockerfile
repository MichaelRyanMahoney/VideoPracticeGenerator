FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VPG_DATA_DIR=/data \
    NLTK_DATA=/app/nltk_data \
    BLENDER_VERSION=4.1.1 \
    BLENDER_DIR=/usr/local/blender

WORKDIR /app

# System deps (ffmpeg, GL/X libs needed by Blender)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl wget xz-utils ffmpeg \
    libglu1-mesa libgl1 libx11-6 libxi6 libxxf86vm1 libxrender1 libxfixes3 libsm6 libxext6 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Blender (Linux x64) headless
RUN mkdir -p ${BLENDER_DIR} /app/nltk_data && \
    wget -q https://mirror.clarkson.edu/blender/release/Blender${BLENDER_VERSION%.*}/blender-${BLENDER_VERSION}-linux-x64.tar.xz -O /tmp/blender.txz && \
    tar -xJf /tmp/blender.txz -C ${BLENDER_DIR} --strip-components=1 && \
    ln -sf ${BLENDER_DIR}/blender /usr/local/bin/blender && \
    rm -f /tmp/blender.txz

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app

# Pipeline Python deps (Torch/WhisperX/g2p/nltk)
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir whisperx g2p_en nltk requests

# Preload NLTK data
RUN python -c "import nltk; nltk.download('averaged_perceptron_tagger_eng'); nltk.download('punkt')"

EXPOSE 8000
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "app.server:app"]

ENV NLTK_DATA=/app/nltk_data
RUN python -c "import nltk; nltk.download('averaged_perceptron_tagger_eng'); nltk.download('punkt')"
