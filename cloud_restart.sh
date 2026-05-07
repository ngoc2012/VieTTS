#!/bin/bash
# Monitor index page health; auto-restart cloudflared on error.
#
# Required env vars:
#   SMTP_USER  — sender Gmail address
#   SMTP_PASS  — Gmail App Password (not account password)
# Optional:
#   SMTP_SERVER (default: smtp.gmail.com)
#   SMTP_PORT   (default: 587)
#   EMAIL_TO    (default: minh@certideal.com)
#   LOCAL_URL   (default: http://localhost:5000)
#   CHECK_INTERVAL (default: 30)
#   RESTART_DELAY (default: 5)

set +e

EMAIL_TO="${EMAIL_TO:-ngoc2012@yahoo.com}"
LOCAL_URL="${LOCAL_URL:-http://localhost:5000}"
CHECK_INTERVAL="${CHECK_INTERVAL:-30}"
RESTART_DELAY="${RESTART_DELAY:-5}"
TUNNEL_URL=""

send_email() {
    local subject="$1"
    local body="$2"

    SUBJECT="$subject" \
    BODY="$body" \
    EMAIL_TO="$EMAIL_TO" \
    python3 - <<'PYEOF'
import smtplib, os, sys
from email.mime.text import MIMEText

subject = os.environ['SUBJECT']
body    = os.environ['BODY']
to      = os.environ['EMAIL_TO']
server  = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
port    = int(os.environ.get('SMTP_PORT', '587'))
user    = os.environ.get('SMTP_USER', '')
passwd  = os.environ.get('SMTP_PASS', '').replace(' ', '')

if not user or not passwd:
    print(f"[EMAIL SKIP] Set SMTP_USER + SMTP_PASS to enable.")
    sys.exit(0)

msg = MIMEText(body)
msg['Subject'] = subject
msg['From']    = user
msg['To']      = to

try:
    with smtplib.SMTP(server, port) as s:
        s.ehlo()
        s.starttls()
        s.login(user, passwd)
        s.send_message(msg)
    print(f"[EMAIL SENT] {subject} -> {to}")
except Exception as e:
    print(f"[EMAIL ERROR] {e}")
PYEOF
}

restart_count=0
cloudflared_pid=""

cleanup() {
    if [ -n "$cloudflared_pid" ] && kill -0 "$cloudflared_pid" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Killing cloudflared (PID $cloudflared_pid)..."
        kill "$cloudflared_pid" 2>/dev/null
    fi
}

trap cleanup EXIT

start_cloudflared() {
    restart_count=$((restart_count + 1))
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting cloudflared (attempt $restart_count)..."

    cloudflared tunnel --url "$LOCAL_URL" > /tmp/cloudflared.log 2>&1 &
    cloudflared_pid=$!

    sleep 2
    while [ $(($(date +%s) - $(date -r /tmp/cloudflared.log +%s 2>/dev/null || echo 0))) -lt 5 ]; do
        if grep -q "https://[a-zA-Z0-9-]*\.trycloudflare\.com" /tmp/cloudflared.log 2>/dev/null; then
            TUNNEL_URL=$(grep -oE "https://[a-zA-Z0-9-]+\.trycloudflare\.com" /tmp/cloudflared.log | head -1)
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Tunnel URL: $TUNNEL_URL"
            break
        fi
        sleep 0.5
    done
}

check_health() {
    if ! curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$LOCAL_URL" 2>/dev/null | grep -q "^[23]"; then
        return 1
    fi
    return 0
}

start_cloudflared

while true; do
    sleep "$CHECK_INTERVAL"

    if ! check_health; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: Index page unreachable ($LOCAL_URL). Restarting..."

        cleanup
        sleep "$RESTART_DELAY"
        start_cloudflared

        error_msg="VieNeu-TTS service failed at $(date '+%Y-%m-%d %H:%M:%S'). Restarted cloudflared.

Tunnel URL: ${TUNNEL_URL:-unknown}
Local URL: $LOCAL_URL
Server: $(uname -n)"

        send_email "VieNeu-TTS ERROR: Service restarted" "$error_msg" &
    elif ! kill -0 "$cloudflared_pid" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: cloudflared process died. Restarting..."
        sleep "$RESTART_DELAY"
        start_cloudflared
    fi
done
