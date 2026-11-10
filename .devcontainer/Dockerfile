# syntax=docker/dockerfile:1
#
# Cloud-Chrome — Phase 1 foundation image.
#
# SCOPE (Phase 1): install and pin the runtime dependencies, then validate them.
# The browser runtime (CLI, supervisor, service lifecycle, auto-start) is
# deliberately NOT part of this image — that is Phase 2. Nothing here starts a
# browser, and no credential material is ever baked into the image.
#
# Build context is the repository root so that config/versions.env and
# .devcontainer/chrome-policies/ are reachable; .dockerignore keeps personal
# browser data, secrets and VCS metadata out of the context.

FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04@sha256:d94c97dd9cacf183d0a6fd12a8e87b526e9e928307674ae9c94139139c0c6eae

ARG DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH

# Checksum verification uses a pipeline, so failures must not be masked.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Single source of truth for every pinned version and checksum.
COPY config/versions.env /tmp/cloudchrome-versions.env

# ---------------------------------------------------------------------------
# System packages.
#
# The TigerVNC packages, websockify and jq are installed with an exact version
# requirement: apt fails the build rather than substituting another version.
#
# The remaining packages come from the signed Ubuntu archive and deliberately
# track its security updates; their versions are recorded in
# config/versions.env and asserted by CI. Pinning them here would break the
# build on every archive refresh without improving reproducibility of the
# components this project actually manages.
# hadolint ignore=DL3008
# ---------------------------------------------------------------------------
RUN set -eux; \
    . /tmp/cloudchrome-versions.env; \
    if [ "${TARGETARCH:-amd64}" != "amd64" ]; then \
        echo "Cloud-Chrome supports linux/amd64 only (got '${TARGETARCH:-unknown}')" >&2; \
        exit 1; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        "tigervnc-standalone-server=${TIGERVNC_VERSION}" \
        "tigervnc-common=${TIGERVNC_VERSION}" \
        "tigervnc-tools=${TIGERVNC_VERSION}" \
        "websockify=${WEBSOCKIFY_VERSION}" \
        "jq=${JQ_VERSION}" \
        xfce4 \
        xfce4-terminal \
        dbus-x11 \
        gnome-keyring \
        libsecret-1-0 \
        libsecret-tools \
        xdotool \
        fonts-liberation2 \
        fonts-noto-color-emoji \
        ca-certificates \
        curl \
        wget; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Google Chrome Stable.
#
# The .deb is fetched directly and its SHA-256 is verified against the recorded
# value before installation. Chrome's own runtime dependencies are resolved by
# apt from the signed Ubuntu archive.
# ---------------------------------------------------------------------------
RUN set -eux; \
    . /tmp/cloudchrome-versions.env; \
    apt-get update; \
    mkdir -p /tmp/dl; \
    curl -fsSL --retry 3 -o /tmp/dl/chrome.deb "${CHROME_DEB_URL}"; \
    echo "${CHROME_DEB_SHA256}  /tmp/dl/chrome.deb" | sha256sum -c -; \
    apt-get install -y --no-install-recommends /tmp/dl/chrome.deb; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /tmp/dl

# ---------------------------------------------------------------------------
# noVNC client assets and the age encryption tool.
#
# Both are upstream release tarballs; both are verified against recorded
# SHA-256 checksums before anything is unpacked.
# ---------------------------------------------------------------------------
RUN set -eux; \
    . /tmp/cloudchrome-versions.env; \
    mkdir -p /tmp/dl /usr/local/share/novnc; \
    curl -fsSL --retry 3 -o /tmp/dl/novnc.tar.gz "${NOVNC_URL}"; \
    echo "${NOVNC_SHA256}  /tmp/dl/novnc.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/dl/novnc.tar.gz -C /tmp/dl; \
    cp -a "/tmp/dl/noVNC-${NOVNC_VERSION}/." /usr/local/share/novnc/; \
    rm -rf "/tmp/dl/noVNC-${NOVNC_VERSION}"; \
    curl -fsSL --retry 3 -o /tmp/dl/age.tar.gz "${AGE_URL}"; \
    echo "${AGE_SHA256}  /tmp/dl/age.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/dl/age.tar.gz -C /tmp/dl; \
    install -m 0755 -o root -g root /tmp/dl/age/age /usr/local/bin/age; \
    install -m 0755 -o root -g root /tmp/dl/age/age-keygen /usr/local/bin/age-keygen; \
    rm -rf /tmp/dl

# ---------------------------------------------------------------------------
# Chrome managed policy (strict isolation, Rev 5 §5).
#
#   SyncDisabled: true  -> Chrome Sync is off by default.
#   BrowserSignin: 0   -> no browser-level Google account; Google *website*
#                        sign-in remains available.
#
# The file is root-owned and must NOT be writable by the runtime user. It is
# intentionally NOT removable at runtime: changing isolation is a source-level
# change followed by a Codespace rebuild.
# ---------------------------------------------------------------------------
COPY .devcontainer/chrome-policies/managed/cloudchrome.json /etc/opt/chrome/policies/managed/cloudchrome.json
RUN set -eux; \
    . /tmp/cloudchrome-versions.env; \
    install -d -m 0755 -o root -g root /etc/opt/chrome/policies/managed; \
    chown root:root /etc/opt/chrome/policies/managed/cloudchrome.json; \
    chmod 0644 /etc/opt/chrome/policies/managed/cloudchrome.json; \
    jq -e '.SyncDisabled == true and .BrowserSignin == 0' \
        /etc/opt/chrome/policies/managed/cloudchrome.json >/dev/null; \
    install -d -m 0755 -o root -g root /usr/local/share/cloudchrome; \
    install -m 0644 -o root -g root /tmp/cloudchrome-versions.env \
        /usr/local/share/cloudchrome/versions.env; \
    install -d -m 0755 -o root -g root /workspaces; \
    rm -f /tmp/cloudchrome-versions.env; \
    if ! id vscode >/dev/null 2>&1; then \
        echo "runtime user 'vscode' is missing from the base image" >&2; \
        exit 1; \
    fi; \
    runuser -u vscode -- test ! -w /etc/opt/chrome/policies/managed/cloudchrome.json; \
    echo "policy is root-owned and not writable by the runtime user"

# ---------------------------------------------------------------------------
# Runtime posture: the container runs as the non-root 'vscode' user.
# ---------------------------------------------------------------------------
USER vscode
WORKDIR /workspaces
ENV LANG=C.UTF-8
