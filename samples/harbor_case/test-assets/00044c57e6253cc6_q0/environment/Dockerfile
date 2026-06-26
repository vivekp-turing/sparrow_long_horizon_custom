FROM ghcr.io/laude-institute/t-bench/ubuntu-24-04:20250624
ENV DEBIAN_FRONTEND="noninteractive"
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip tmux asciinema git curl && rm -rf /var/lib/apt/lists/*
RUN python3 -m pip install --no-cache-dir --break-system-packages pandas==2.3.2 pytest==8.4.1
RUN mkdir -p "/app"
COPY workspace.tar.gz /tmp/workspace.tar.gz
RUN tar xzf /tmp/workspace.tar.gz --dereference --strip-components=1 -C /app && rm /tmp/workspace.tar.gz
RUN cd /app && for f in _dot_*; do [ -e "$f" ] && mv "$f" "${f#_dot_}"; done || true
RUN if [ -d "/app/rootfs" ]; then rm -rf /app/rootfs/tmp /app/rootfs/etc/passwd /app/rootfs/etc/shadow /app/rootfs/etc/group 2>/dev/null; cp -a /app/rootfs/. / 2>/dev/null; rm -rf /app/rootfs; fi || true
# Drop donor Claude Code configs from workspace/rootfs snapshots. Agent-Service
# injects the intended model at runtime; persisted Claude settings can override it.
RUN rm -rf /root/.claude /root/.claude.json /root/.config/claude-code /root/.cache/claude-code && \
    find /app \( -path '*/.claude' -o -name '.claude.json' -o -name '.mcp.json' \) -prune -exec rm -rf {} +
RUN rm -rf /logs && mkdir -p /logs/agent /logs/verifier /logs/artifacts
RUN ln -sf "/usr/bin/python3" "/usr/bin/python"
RUN mkdir -p "/usr/bin" && \
    ln -sfn "/usr/bin/python3" "/usr/bin/python"
WORKDIR /app
