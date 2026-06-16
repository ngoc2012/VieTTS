#!/bin/bash
# Monitor index page health; auto-restart ngrok on error.
#
# Required env vars:
#   SMTP_USER  — sender Gmail address
#   SMTP_PASS  — Gmail App Password (not account password)
# Optional:
#   SMTP_SERVER (default: smtp.gmail.com)
#   SMTP_PORT   (default: 587)
#   EMAIL_TO    (default: minh@certideal.com)
#   LOCAL_URL   (default: http://localhost:5000)
#   CHECK_INTERVAL (default: 600)
#   RESTART_DELAY (default: 5)

set +e

EMAIL_TO="${EMAIL_TO:-ngoc2012@yahoo.com}"
LOCAL_URL="${LOCAL_URL:-http://localhost:5000}"
CHECK_INTERVAL="${CHECK_INTERVAL:-600}"
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
ngrok_pid=""
BASE_INTERVAL="$CHECK_INTERVAL"
double_count=0

cleanup() {
    if [ -n "$ngrok_pid" ] && kill -0 "$ngrok_pid" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Killing ngrok (PID $ngrok_pid)..."
        kill "$ngrok_pid" 2>/dev/null
    fi
}

trap cleanup EXIT

start_ngrok() {
    restart_count=$((restart_count + 1))
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting ngrok (attempt $restart_count)..."

    ngrok http 5000 > /tmp/ngrok.log 2>&1 &
    ngrok_pid=$!

    local deadline=$(($(date +%s) + 30))
    while [ $(date +%s) -lt $deadline ]; do
        TUNNEL_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null \
            | python3 -c "import sys,json; t=json.load(sys.stdin).get('tunnels',[]); print(next((x['public_url'] for x in t if x['public_url'].startswith('https')), ''))" 2>/dev/null)
        if [ -n "$TUNNEL_URL" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Tunnel URL: $TUNNEL_URL"
            break
        fi
        sleep 1
    done
}

check_health() {
    if ! curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$LOCAL_URL" 2>/dev/null | grep -q "^[23]"; then
        return 1
    fi
    if [ -n "$TUNNEL_URL" ]; then
        if ! curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 "$TUNNEL_URL" 2>/dev/null | grep -q "^[23]"; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN: Tunnel URL unreachable ($TUNNEL_URL)"
            return 1
        fi
    fi
    return 0
}

start_ngrok

start_msg="VieNeu-TTS started at $(date '+%Y-%m-%d %H:%M:%S').

Tunnel URL: ${TUNNEL_URL:-unknown}
Local URL: $LOCAL_URL
Server: $(uname -n)"
send_email "VieNeu-TTS STARTED: Tunnel up" "$start_msg" &

while true; do
    sleep "$CHECK_INTERVAL"

    fail_count=0
    if ! check_health; then
        fail_count=1
        for i in 2 3 4 5; do
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN: Check $i/5 failed, retrying in $CHECK_INTERVAL s..."
            sleep "$CHECK_INTERVAL"
            if check_health; then
                fail_count=0
                break
            fi
            fail_count=$i
        done
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] INFO: $fail_count fails."
    fi

    if [ "$fail_count" -ge 5 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: Index page unreachable 3/3 checks ($LOCAL_URL). Restarting..."

        cleanup
        sleep "$RESTART_DELAY"
        start_ngrok

        error_msg="VieNeu-TTS service failed at $(date '+%Y-%m-%d %H:%M:%S'). Restarted ngrok.

Tunnel URL: ${TUNNEL_URL:-unknown}
Local URL: $LOCAL_URL
Server: $(uname -n)"

        send_email "VieNeu-TTS ERROR: Service restarted" "$error_msg" &
    elif ! kill -0 "$ngrok_pid" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: ngrok process died. Restarting..."
        sleep "$RESTART_DELAY"
        start_ngrok
    fi
done
