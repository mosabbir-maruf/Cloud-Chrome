# syntax=docker/dockerfile:1
#
# Cloud-Chrome — dev container image.
#
# SCOPE: install and pin the runtime dependencies, harden the runtime user's
# privileges, and validate the result. The browser runtime (CLI, supervisor,
# service lifecycle) is not part of this image yet.
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
# PINNED_* packages are installed with an exact version requirement: apt fails
# the build rather than substituting another version. TRACKED_* packages are
# installed from the signed Ubuntu archive without an exact requirement so
# that archive security updates are picked up without a rebuild.
#
# DL3008 (pin every apt package) is disabled for this file on purpose; CI
# asserts that the pinned packages stay pinned and that the tracked packages
# are never older than the version recorded in config/versions.env.
# ---------------------------------------------------------------------------
RUN set -eux; \
    . /tmp/cloudchrome-versions.env; \
    if [ "${TARGETARCH:-amd64}" != "amd64" ]; then \
        echo "Cloud-Chrome supports linux/amd64 only (got '${TARGETARCH:-unknown}')" >&2; \
        exit 1; \
    fi; \
    for attempt in 1 2 3 4 5; do \
        if apt-get update; then break; fi; \
        if [ "$attempt" -eq 5 ]; then \
            echo "the Ubuntu archive could not be reached after five attempts" >&2; \
            exit 1; \
        fi; \
        echo "the archive could not be reached; retrying" >&2; \
        sleep 10; \
    done; \
    apt-get install -y --no-install-recommends \
        "tigervnc-standalone-server=${PINNED_TIGERVNC_VERSION}" \
        "tigervnc-common=${PINNED_TIGERVNC_VERSION}" \
        "tigervnc-tools=${PINNED_TIGERVNC_VERSION}" \
        "websockify=${PINNED_WEBSOCKIFY_VERSION}" \
        "jq=${PINNED_JQ_VERSION}" \
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
    for attempt in 1 2 3 4 5; do \
        if apt-get update; then break; fi; \
        if [ "$attempt" -eq 5 ]; then \
            echo "the Ubuntu archive could not be reached after five attempts" >&2; \
            exit 1; \
        fi; \
        echo "the archive could not be reached; retrying" >&2; \
        sleep 10; \
    done; \
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
# The Cloud-Chrome runtime.
#
# The single CLI entry point is bin/cloudchrome. It is installed together
# with the implementation package it loads, and /usr/local/bin/cloudchrome is
# a symlink to it: the launcher contains no logic of its own and delegates to
# the entry point.
#
# The source of truth is the repository; CI asserts that the installed copy
# is byte for byte identical to it, so the two can never drift apart.
# ---------------------------------------------------------------------------
COPY bin/ /usr/local/lib/cloudchrome/bin/
COPY lib/ /usr/local/lib/cloudchrome/lib/
RUN set -eux; \
    chmod 0755 /usr/local/lib/cloudchrome/bin/cloudchrome; \
    find /usr/local/lib/cloudchrome/lib -type d -exec chmod 0755 {} +; \
    find /usr/local/lib/cloudchrome/lib -type f -name '*.py' -exec chmod 0644 {} +; \
    ln -sfn /usr/local/lib/cloudchrome/bin/cloudchrome /usr/local/bin/cloudchrome; \
    /usr/local/bin/cloudchrome version

# ---------------------------------------------------------------------------
# Chrome managed policy (strict isolation).
#
#   SyncDisabled: true  -> Chrome Sync is off by default.
#   BrowserSignin: 0   -> no browser-level Google account; Google *website*
#                        sign-in remains available.
#
# The file is root-owned and must NOT be writable or removable by the runtime
# user. It is intentionally NOT removable at runtime: changing isolation is a
# source-level change followed by a Codespace rebuild.
# ---------------------------------------------------------------------------
COPY .devcontainer/chrome-policies/managed/cloudchrome.json /etc/opt/chrome/policies/managed/cloudchrome.json
COPY .devcontainer/cloudchrome.env /etc/cloudchrome/cloudchrome.env
RUN set -eux; \
    . /tmp/cloudchrome-versions.env; \
    install -d -m 0755 -o root -g root /etc/opt/chrome/policies/managed; \
    install -d -m 0755 -o root -g root /etc/cloudchrome; \
    chown root:root /etc/opt/chrome/policies/managed/cloudchrome.json; \
    chmod 0644 /etc/opt/chrome/policies/managed/cloudchrome.json; \
    chown root:root /etc/cloudchrome/cloudchrome.env; \
    chmod 0644 /etc/cloudchrome/cloudchrome.env; \
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
    fi

# ---------------------------------------------------------------------------
# Runtime privilege reduction.
#
# The base image grants the runtime user unrestricted passwordless root through
# /etc/sudoers.d/vscode ("vscode ALL=(root) NOPASSWD:ALL"). That would let the
# runtime user rewrite or delete the managed Chrome policy, so the blanket
# entry is removed. The sudo binary is kept, but the runtime user holds no
# sudo rule at all: `sudo -n true` must fail.
#
# Everything the image needs is installed at build time, so no runtime package
# installation is required.
# ---------------------------------------------------------------------------
RUN set -eux; \
    rm -f /etc/sudoers.d/vscode; \
    if runuser -u vscode -- sudo -n true 2>/dev/null; then \
        echo "the runtime user still has passwordless root" >&2; \
        exit 1; \
    fi; \
    if runuser -u vscode -- sudo -n -l 2>/dev/null | grep -q "ALL"; then \
        echo "the runtime user still has an ALL sudo rule" >&2; \
        exit 1; \
    fi; \
    if runuser -u vscode -- test -w /etc/opt/chrome/policies/managed/cloudchrome.json; then \
        echo "the runtime user can write the managed Chrome policy" >&2; \
        exit 1; \
    fi; \
    if runuser -u vscode -- test -w /etc/opt/chrome/policies/managed; then \
        echo "the runtime user can replace the managed Chrome policy" >&2; \
        exit 1; \
    fi; \
    echo "runtime user has no sudo rule and cannot modify the managed Chrome policy"

# ---------------------------------------------------------------------------
# Runtime posture: the container runs as the non-root 'vscode' user.
# ---------------------------------------------------------------------------
USER vscode
WORKDIR /workspaces
ENV LANG=C.UTF-8
