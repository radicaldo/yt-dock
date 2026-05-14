ARG ENABLE_CUDA=false

FROM python:3.12.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Install PyTorch first — CPU-only by default, CUDA when opted in.
# Installing before requirements.txt means sentence-transformers
# will see torch already satisfied and won't pull the CUDA variant.
ARG ENABLE_CUDA
RUN if [ "$ENABLE_CUDA" = "true" ]; then \
      echo ">>> Installing PyTorch with CUDA support" && \
      pip install --no-cache-dir torch; \
    else \
      echo ">>> Installing PyTorch CPU-only" && \
      pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu; \
    fi

RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir -r requirements.txt && \
    python -c "import nltk; nltk.download('punkt', quiet=True); nltk.download('punkt_tab', quiet=True)"

# Pre-download the embedding model so first startup is instant
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY app/ .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
