#!/bin/bash
# Auto-restart cloudflared tunnel; email new URL on each start.
#
# Required env vars:
#   SMTP_USER  — sender Gmail address
#   SMTP_PASS  — Gmail App Password (not account password)
# Optional:
#   SMTP_SERVER (default: smtp.gmail.com)
#   SMTP_PORT   (default: 587)
#   EMAIL_TO    (default: minh@certideal.com)
#   LOCAL_URL   (default: http://localhost:5000)
#   RESTART_DELAY (default: 5)

set +e

EMAIL_TO="${EMAIL_TO:-ngoc2012@yahoo.com}"
LOCAL_URL="${LOCAL_URL:-http://localhost:5000}"
RESTART_DELAY="${RESTART_DELAY:-5}"

send_email() {
    local url="$1"
    TUNNEL_URL="$url" \
    EMAIL_TO="$EMAIL_TO" \
    python3 - <<'PYEOF'
import smtplib, os, sys
from email.mime.text import MIMEText

url    = os.environ['TUNNEL_URL']
to     = os.environ['EMAIL_TO']
server = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
port   = int(os.environ.get('SMTP_PORT', '587'))
user   = os.environ.get('SMTP_USER', '')
passwd = os.environ.get('SMTP_PASS', '').replace(' ', '')

if not user or not passwd:
    print(f"[EMAIL SKIP] Set SMTP_USER + SMTP_PASS to enable. URL: {url}")
    sys.exit(0)

body = f"VieNeu-TTS tunnel (re)started.\n\nURL: {url}\n\nServer: {os.uname().nodename}"
msg = MIMEText(body)
msg['Subject'] = f'VieNeu-TTS tunnel: {url}'
msg['From']    = user
msg['To']      = to

with smtplib.SMTP(server, port) as s:
    s.ehlo()
    s.starttls()
    s.login(user, passwd)
    s.send_message(msg)
print(f"[EMAIL SENT] {url} -> {to}")
PYEOF
}

restart_count=0

while true; do
    restart_count=$((restart_count + 1))
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting cloudflared (attempt $restart_count)..."

    while IFS= read -r line; do
        echo "$line"
        if [[ "$line" =~ (https://[a-zA-Z0-9-]+\.trycloudflare\.com) ]]; then
            url="${BASH_REMATCH[1]}"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Tunnel URL: $url"
            send_email "$url" &
        fi
    done < <(cloudflared tunnel --url "$LOCAL_URL" 2>&1)

    exit_code=$?
    if [ "$exit_code" -eq 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] cloudflared exited cleanly (code 0). Stopping."
        exit 0
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] cloudflared exited (code $exit_code). Restarting in ${RESTART_DELAY}s..."
    sleep "$RESTART_DELAY"
done
