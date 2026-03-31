# Deployment Guide for FundamentalSignals ORACLE v2.1

This guide provides instructions for building, checking, and deploying the FundamentalSignals system.

## 1. Build & Check (Development)

Before deploying, ensure the environment is correctly set up:

```bash
# Install dependencies
pip install -r requirements.txt

# Run smoke test
python smoke_test.py
```

## 2. Configuration

Ensure `config/settings.json` is configured:
-   `zai.api_key`: Your Z.ai API key (mandatory for Layer 2 signals).
-   `server.host`: Set to `0.0.0.0` for external access.

## 3. Deployment Options

### Option A: Docker (Recommended)

The easiest way to deploy consistently across environments.

```bash
# Build and start
docker-compose up -d --build

# View logs
docker-compose logs -f
```

### Option B: Systemd (VPS Direct Deployment)

For running directly on a Linux VPS.

1. Create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Create `/etc/systemd/system/oracle.service`:
   ```ini
   [Unit]
   Description=FundamentalSignals ORACLE v2.1
   After=network.target

   [Service]
   User=youruser
   WorkingDirectory=/path/to/FundamentalSignals
   ExecStart=/path/to/FundamentalSignals/.venv/bin/python run.py
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```
3. Start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable oracle
   sudo systemctl start oracle
   ```

### Option C: Reverse Proxy (Nginx)

Recommended for SSL and production-grade stability.

```nginx
server {
    listen 80;
    server_name oracle.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## 4. Post-Deployment Check

-   **Dashboard**: `http://<your-ip>:8000/`
-   **Health API**: `http://<your-ip>:8000/api/health`
-   **Logs**: Check `logs/oracle.log` for runtime events.
