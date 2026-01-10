FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VPG_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app

EXPOSE 8000
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "app.server:app"]

ENV NLTK_DATA=/app/nltk_data
RUN python -c "import nltk; nltk.download('averaged_perceptron_tagger_eng'); nltk.download('punkt')"
