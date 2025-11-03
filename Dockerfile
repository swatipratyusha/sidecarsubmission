# Use vanilla Python as base - most flexible approach
FROM python:3.12-slim-bookworm

# Avoid prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Set display environment
ENV DISPLAY=:99
ENV SCREEN_WIDTH=1920
ENV SCREEN_HEIGHT=1080
ENV SCREEN_DEPTH=24

# Install system dependencies for VNC and browsers
RUN apt-get update && apt-get install -y \
    # VNC Server and X11 components
    x11vnc \
    xvfb \
    fluxbox \
    xterm \
    x11-utils \
    # Browser dependencies
    wget \
    curl \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatspi2.0-0 \
    libdrm2 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
    # noVNC for browser-based VNC access
    novnc \
    websockify \
    # Clean up
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers - Chrome is the real Google Chrome!
RUN playwright install --with-deps chrome firefox
# Also install chromium as fallback
RUN playwright install chromium

# Create VNC password file
RUN mkdir -p /root/.vnc && \
    x11vnc -storepasswd secret /root/.vnc/passwd

# Create directories for cache and output
RUN mkdir -p /app/cache /app/output

# Copy application files
COPY ./src .
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Expose VNC ports and Flask API port
EXPOSE 5900 6080 5000

# Start script will handle VNC server and run the app
ENTRYPOINT ["/start.sh"] 