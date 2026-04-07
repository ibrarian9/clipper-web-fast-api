#!/bin/bash
# ─────────────────────────────────────────────────
# Local dev setup for Clipper
# Run: chmod +x setup_local.sh && ./setup_local.sh
# ─────────────────────────────────────────────────
set -e

echo "═══ Clipper Local Setup ═══"

# 1. Start services
echo "[1/5] Starting MySQL + Redis..."
sudo systemctl start mysql
sudo systemctl start redis-server

# 2. Create MySQL database & user
echo "[2/5] Setting up MySQL database..."
sudo mysql -e "
  CREATE DATABASE IF NOT EXISTS clipper;
  CREATE USER IF NOT EXISTS 'clipper'@'localhost' IDENTIFIED BY 'clipper';
  GRANT ALL PRIVILEGES ON clipper.* TO 'clipper'@'localhost';
  FLUSH PRIVILEGES;
" 2>/dev/null || echo "  (DB may already exist, skipping)"

# 3. Create .env if missing
echo "[3/5] Creating .env..."
if [ ! -f .env ]; then
  cp .env.example .env
  # Fix DATABASE_URL for local (localhost, not Docker host)
  sed -i 's|host.docker.internal|localhost|g' .env
  echo "  .env created from .env.example"
else
  echo "  .env already exists, skipping"
fi

# 4. Install Python deps
echo "[4/5] Installing Python dependencies..."
source venv/bin/activate
pip install -q -r requirements.txt

# 5. Create storage dirs
echo "[5/5] Creating storage directories..."
sudo mkdir -p /opt/clipper/storage/{downloads,final,screenshots}
sudo mkdir -p /opt/clipper/cookies
sudo chown -R $USER:$USER /opt/clipper

echo ""
echo "═══ Setup Complete! ═══"
echo ""
echo "To run the app, open 2 terminals:"
echo ""
echo "  Terminal 1 (Web Server):"
echo "    cd $(pwd)"
echo "    source venv/bin/activate"
echo "    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"
echo ""
echo "  Terminal 2 (Celery Worker):"
echo "    cd $(pwd)"
echo "    source venv/bin/activate"
echo "    celery -A app.tasks worker --loglevel=info --concurrency=1"
echo ""
echo "Then open: http://localhost:8000"
