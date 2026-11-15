FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/tmp/uv-cache \
    RCLONE_CONFIG=/config/rclone/rclone.conf \
    RCLONE_RUNNER_DATA_DIR=/data \
    RCLONE_RUNNER_LOG_DIR=/data/logs \
    RCLONE_RUNNER_DATABASE_URL=sqlite:////data/rclone-runner.db

# Bump this to update rclone; changing it invalidates the layer below.
# Not named RCLONE_*: rclone reads RCLONE_<FLAG> env vars as flags.
ARG BUNDLED_RCLONE_VERSION=1.76.0

# rclone's release signing key, fingerprint FBF737ECE9F8AB18604BD2AC93935E02FF3B54FA
# (see https://rclone.org/release_signing/). gpgv ships with the base image.
COPY docker/rclone-release-key.asc /tmp/rclone-release-key.asc

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64|arm64|386) ;; \
        armhf) arch=arm-v7 ;; \
        i386) arch=386 ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    zip="rclone-v${BUNDLED_RCLONE_VERSION}-linux-${arch}.zip"; \
    base="https://downloads.rclone.org/v${BUNDLED_RCLONE_VERSION}"; \
    tmp="$(mktemp -d)"; \
    cd "$tmp"; \
    curl -fsSLO "$base/$zip"; \
    curl -fsSLO "$base/SHA256SUMS"; \
    sed -e '1,/^$/d' -e '/^=/,$d' /tmp/rclone-release-key.asc | base64 -d > rclone.gpg; \
    gpgv --keyring ./rclone.gpg --output SHA256SUMS.verified SHA256SUMS; \
    grep " ${zip}\$" SHA256SUMS.verified | sha256sum -c -; \
    unzip -q "$zip"; \
    install -m 0755 "${zip%.zip}/rclone" /usr/local/bin/rclone; \
    cd /; \
    rm -rf "$tmp" /tmp/rclone-release-key.asc; \
    rclone version; \
    apt-get purge -y --auto-remove curl unzip; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

# Run as an unprivileged user. 1000:1000 is only the image default: Compose sets the real user
# from RCLONE_RUNNER_UID/RCLONE_RUNNER_GID, and the host folders mounted at /data and
# /config/rclone must be writable by that user. HOME gives rclone a writable cache folder.
RUN mkdir -p /data/logs /config/rclone /media \
    && chown -R 1000:1000 /data /config/rclone
ENV HOME=/data \
    PATH="/app/.venv/bin:$PATH"
USER 1000:1000

EXPOSE 8000
VOLUME ["/data", "/config/rclone", "/media"]
# Start uvicorn from the virtualenv directly: `uv run` would need uv's root-owned build cache.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
