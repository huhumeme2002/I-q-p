#!/bin/bash
set -e

APP_DIR="/opt/iflow"
APP_USER="iflow"
SERVICE_NAME="iflow-proxy"

INSTALL_REG=false
if [[ "$1" == "--with-reg" ]]; then
    INSTALL_REG=true
fi

echo "=== iFlow Proxy - Ubuntu VPS Deployment ==="

# 1. System dependencies
echo "[1/7] Installing system dependencies..."
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git

# 2. Create user
if ! id "$APP_USER" &>/dev/null; then
    echo "[2/7] Creating user '$APP_USER'..."
    sudo useradd -r -m -s /bin/bash "$APP_USER"
else
    echo "[2/7] User '$APP_USER' already exists"
fi

# 3. Copy project files
echo "[3/7] Setting up project directory..."
sudo mkdir -p "$APP_DIR"
sudo cp proxy.py store.py iflow_auth.py requirements.txt "$APP_DIR/"
sudo cp -r static "$APP_DIR/" 2>/dev/null || true
if $INSTALL_REG; then
    sudo cp reg_iflow.py "$APP_DIR/" 2>/dev/null || true
fi
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# 4. Python venv + dependencies
echo "[4/7] Creating venv and installing dependencies..."
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# 5. Playwright for headless registration (optional)
if $INSTALL_REG; then
    echo "[5/7] Installing Playwright for headless registration..."
    # Playwright system deps (Chromium needs these on headless Ubuntu)
    sudo apt install -y \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libxshmfence1 libx11-xcb1 \
        libxfixes3 fonts-liberation xvfb
    sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install playwright
    sudo -u "$APP_USER" "$APP_DIR/venv/bin/playwright" install chromium
else
    echo "[5/7] Skipping Playwright (use --with-reg to install)"
fi

# 6. Install systemd service
echo "[6/7] Installing systemd service..."
sudo cp iflow-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# 7. Start
echo "[7/7] Starting service..."
sudo systemctl start "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager

echo ""
echo "=== Done ==="
echo "  Service : sudo systemctl status $SERVICE_NAME"
echo "  Logs    : sudo journalctl -u $SERVICE_NAME -f"
echo "  Admin   : http://<your-vps-ip>:8083/admin"
echo ""
echo "  Claude Code setup:"
echo "  export ANTHROPIC_BASE_URL=http://<your-vps-ip>:8083"
echo "  export ANTHROPIC_API_KEY=dummy"
echo "  claude"
if $INSTALL_REG; then
    echo ""
    echo "  Registration (headless):"
    echo "  sudo -u $APP_USER xvfb-run $APP_DIR/venv/bin/python $APP_DIR/reg_iflow.py"
    echo "  Or launch from admin UI: http://<your-vps-ip>:8083/admin"
fi
