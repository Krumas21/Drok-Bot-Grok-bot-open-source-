# The agents' shared computer: virtual screens with a real Chromium, a shell with sudo, and common dev tools.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc fluxbox xdotool scrot socat chromium \
        fonts-dejavu-core fonts-liberation fonts-noto-color-emoji \
        ca-certificates curl wget git jq unzip zip procps sudo less nano xterm \
        python3 python3-pip python3-venv nodejs npm build-essential \
    && rm -rf /var/lib/apt/lists/*

# The container is the sandbox: agents get passwordless sudo inside it, nothing outside it.
RUN useradd -m -u 1000 -s /bin/bash drok \
    && echo "drok ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/drok \
    && mkdir -p /workspace && chown drok:drok /workspace

COPY computerd.py /opt/computerd.py
USER drok
WORKDIR /workspace
EXPOSE 7000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fs http://localhost:7000/health | grep -q '"ok": true'
CMD ["python3", "/opt/computerd.py"]
