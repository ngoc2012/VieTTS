#!/bin/bash
# Monitor health; auto-restart tunnel on error.
# Priority: ngrok → cloudflared (fallback on bandwidth limit or startup failure)
#
# Required env vars:
#   SMTP_USER  — sender Gmail address
#   SMTP_PASS  — Gmail App Password (not account password)
# Optional:
#   SMTP_SERVER (default: smtp.gmail.com)
#   SMTP_PORT   (default: 587)
#   EMAIL_TO    (default: ngoc2012@yahoo.com)
#   LOCAL_URL   (default: http://localhost:5000)
#   CHECK_INTERVAL (default: 600)
#   RESTART_DELAY (default: 5)

set +e

EMAIL_TO="${EMAIL_TO:-ngoc2012@yahoo.com}"
LOCAL_URL="${LOCAL_URL:-http://localhost:5000}"
CHECK_INTERVAL="${CHECK_INTERVAL:-600}"
RESTART_DELAY="${RESTART_DELAY:-5}"
TUNNEL_URL=""
ACTIVE_TUNNEL=""   # "ngrok" or "cloudflared"
tunnel_pid=""
restart_count=0

# ── email ────────────────────────────────────────────────────────────────────

send_email() {
    local subject="$1"
    local body="$2"
    SUBJECT="$subject" BODY="$body" EMAIL_TO="$EMAIL_TO" \
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
    print("[EMAIL SKIP] Set SMTP_USER + SMTP_PASS to enable.")
    sys.exit(0)

msg = MIMEText(body)
msg['Subject'] = subject
msg['From']    = user
msg['To']      = to

try:
    with smtplib.SMTP(server, port) as s:
        s.ehlo(); s.starttls(); s.login(user, passwd); s.send_message(msg)
    print(f"[EMAIL SENT] {subject} -> {to}")
except Exception as e:
    print(f"[EMAIL ERROR] {e}")
PYEOF
}

# ── cleanup ───────────────────────────────────────────────────────────────────

cleanup() {
    if [ -n "$tunnel_pid" ] && kill -0 "$tunnel_pid" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Killing $ACTIVE_TUNNEL (PID $tunnel_pid)..."
        kill "$tunnel_pid" 2>/dev/null
    fi
}

trap cleanup EXIT

# ── ngrok bandwidth detection ─────────────────────────────────────────────────

ngrok_has_bandwidth_error() {
    # ERR_NGROK_8012 = bandwidth limit; also catch HTTP 429 on tunnel URL
    if grep -qE "ERR_NGROK_8012|bandwidth|rate limit" /tmp/ngrok.log 2>/dev/null; then
        return 0
    fi
    if [ -n "$TUNNEL_URL" ]; then
        local code
        code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 "$TUNNEL_URL" 2>/dev/null)
        [ "$code" = "429" ] && return 0
    fi
    return 1
}

# ── tunnel starters ───────────────────────────────────────────────────────────

start_ngrok() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting ngrok..."
    : > /tmp/ngrok.log
    ngrok http 5000 > /tmp/ngrok.log 2>&1 &
    tunnel_pid=$!
    ACTIVE_TUNNEL="ngrok"
    TUNNEL_URL=""

    local deadline=$(($(date +%s) + 30))
    while [ $(date +%s) -lt $deadline ]; do
        TUNNEL_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null \
            | python3 -c "import sys,json; t=json.load(sys.stdin).get('tunnels',[]); print(next((x['public_url'] for x in t if x['public_url'].startswith('https')), ''))" 2>/dev/null)
        [ -n "$TUNNEL_URL" ] && break
        sleep 1
    done

    if [ -z "$TUNNEL_URL" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN: ngrok failed to get URL."
        return 1
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ngrok URL: $TUNNEL_URL"
    return 0
}

start_cloudflared() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting cloudflared..."
    : > /tmp/cloudflared.log
    cloudflared tunnel --url "$LOCAL_URL" > /tmp/cloudflared.log 2>&1 &
    tunnel_pid=$!
    ACTIVE_TUNNEL="cloudflared"
    TUNNEL_URL=""

    local deadline=$(($(date +%s) + 30))
    while [ $(date +%s) -lt $deadline ]; do
        if grep -q "https://[a-zA-Z0-9-]*\.trycloudflare\.com" /tmp/cloudflared.log 2>/dev/null; then
            TUNNEL_URL=$(grep -oE "https://[a-zA-Z0-9-]+\.trycloudflare\.com" /tmp/cloudflared.log | head -1)
            break
        fi
        sleep 1
    done

    if [ -z "$TUNNEL_URL" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN: cloudflared failed to get URL."
        return 1
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] cloudflared URL: $TUNNEL_URL"
    return 0
}

# Try ngrok first; fall back to cloudflared on failure or bandwidth error.
# Always ngrok-first regardless of which tunnel was previously active.
try_ngrok_then_cloudflared() {
    if start_ngrok; then
        sleep 3
        if ngrok_has_bandwidth_error; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ngrok bandwidth limit hit. Falling back to cloudflared."
            cleanup
            start_cloudflared || echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: cloudflared also failed."
        fi
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ngrok startup failed. Falling back to cloudflared."
        cleanup
        start_cloudflared || echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: cloudflared also failed."
    fi
}

start_tunnel() {
    restart_count=$((restart_count + 1))
    cleanup
    try_ngrok_then_cloudflared
}

# ── health check ──────────────────────────────────────────────────────────────

check_health() {
    if ! curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$LOCAL_URL" 2>/dev/null | grep -q "^[23]"; then
        return 1
    fi
    if [ -n "$TUNNEL_URL" ]; then
        local code
        code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 "$TUNNEL_URL" 2>/dev/null)
        if ! echo "$code" | grep -q "^[23]"; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN: Tunnel unreachable ($TUNNEL_URL) code=$code"
            if [ "$ACTIVE_TUNNEL" = "ngrok" ] && [ "$code" = "429" ]; then
                # ngrok bandwidth limit → switch to cloudflared immediately
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] ngrok bandwidth limit (429). Switching to cloudflared..."
                cleanup
                start_cloudflared
            elif [ "$ACTIVE_TUNNEL" = "cloudflared" ]; then
                # cloudflared session failed → try ngrok first before new cloudflared
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] cloudflared unreachable. Trying ngrok first..."
                cleanup
                try_ngrok_then_cloudflared
            fi
            return 1
        fi
    fi
    return 0
}

# ── main ──────────────────────────────────────────────────────────────────────

start_tunnel

send_email "VieNeu-TTS STARTED: Tunnel up ($ACTIVE_TUNNEL)" \
"VieNeu-TTS started at $(date '+%Y-%m-%d %H:%M:%S').

Active tunnel: $ACTIVE_TUNNEL
Tunnel URL: ${TUNNEL_URL:-unknown}
Local URL: $LOCAL_URL
Server: $(uname -n)" &

while true; do
    sleep "$CHECK_INTERVAL"

    fail_count=0
    if ! check_health; then
        fail_count=1
        for i in 2 3 4 5; do
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN: Check $i/5 failed, retrying in ${CHECK_INTERVAL}s..."
            sleep "$CHECK_INTERVAL"
            if check_health; then
                fail_count=0
                break
            fi
            fail_count=$i
        done
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] INFO: $fail_count consecutive fails."
    fi

    if [ "$fail_count" -ge 5 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: 5 consecutive health check failures. Restarting tunnel..."
        start_tunnel
        send_email "VieNeu-TTS ERROR: Tunnel restarted ($ACTIVE_TUNNEL)" \
"VieNeu-TTS tunnel restarted at $(date '+%Y-%m-%d %H:%M:%S').

Active tunnel: $ACTIVE_TUNNEL
Tunnel URL: ${TUNNEL_URL:-unknown}
Local URL: $LOCAL_URL
Server: $(uname -n)" &

    elif ! kill -0 "$tunnel_pid" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $ACTIVE_TUNNEL process died. Restarting..."
        sleep "$RESTART_DELAY"
        start_tunnel
    fi
done
