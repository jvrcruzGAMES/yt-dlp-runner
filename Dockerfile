FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOWNLOADS_DIR=/app/downloads \
    COOKIES_DIR=/app/cookies \
    DENO_INSTALL=/usr/local \
    PATH="/usr/local/bin:${PATH}"

# Install ffmpeg, curl, unzip, git, ca-certificates, chromium, xvfb, nodejs, npm and native canvas build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    git \
    ca-certificates \
    chromium \
    chromium-driver \
    xvfb \
    nodejs \
    npm \
    build-essential \
    libcairo2-dev \
    libpango1.0-dev \
    libjpeg-dev \
    libgif-dev \
    librsvg2-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Deno JavaScript runtime
RUN curl -fsSL https://deno.land/install.sh | sh \
    && deno --version

# Create isolated cookies and visible downloads directories
RUN mkdir -p /app/downloads /app/cookies

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build official Brainicism/bgutil-ytdlp-pot-provider server natively according to README
RUN cd /app/app/bgutil \
    && npm install \
    && npx tsc

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8080/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
