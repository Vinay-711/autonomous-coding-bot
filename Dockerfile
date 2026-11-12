# ==============================================================================
# Multi-stage Dockerfile for Cloud-Native Autonomous Coding Worker
# Supports: Google Cloud Run Jobs, Kubernetes, and local Linux containers
# Includes: Python 3.11 runtime, OpenJDK 17 JDK (javac & java), Git, and Playwright
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Builder (Compile dependencies and wheel packages)
# ------------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    python3-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ------------------------------------------------------------------------------
# Stage 2: Runtime (Slim production image with OpenJDK 17 & non-root user)
# ------------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

# Install OpenJDK 17 JDK, Git, libpq, libatomic1, and CA certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jdk-headless \
    git \
    libpq5 \
    ca-certificates \
    curl \
    libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Configure architecture-independent JAVA_HOME
RUN ln -s $(dirname $(dirname $(readlink -f $(which javac)))) /usr/lib/jvm/default-java
ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PATH="/usr/lib/jvm/default-java/bin:/opt/venv/bin:$PATH"

# Copy virtual environment from builder stage
COPY --from=builder /opt/venv /opt/venv

# Create unprivileged non-root user and group
RUN groupadd -g 10001 autocoder && \
    useradd -u 10001 -g autocoder -m -s /bin/bash autocoder

# Create writable data directories
WORKDIR /app
RUN mkdir -p /app/data /app/data/solutions /app/data/logs /app/data/repos /app/data/chrome_user_data && \
    chown -R autocoder:autocoder /app

# Switch to non-root user
USER autocoder

# Install Playwright browser dependencies for non-root user
RUN python3 -m playwright install chromium

# Copy application source code
COPY --chown=autocoder:autocoder bot.py pyproject.toml README.md ./

# Set environment variables for cloud container execution
ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    SOLUTIONS_DIR=/app/data/solutions \
    LOGS_DIR=/app/data/logs \
    REPOS_DIR=/app/data/repos \
    CHROME_USER_DATA_DIR=/app/data/chrome_user_data \
    DB_PATH=/app/data/autocoder.db \
    STRUCTURED_LOGGING=true \
    HOME=/app

ENTRYPOINT ["python3", "bot.py"]
CMD ["--platform", "all", "--count", "5", "--mode", "both"]
