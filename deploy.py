#!/usr/bin/env python3
import threading
import requests
import json
import secrets
import string
import os
import subprocess
import sys
import time

API = "https://api.cloudflare.com/client/v4"
ACCOUNT = "c975a170b2d36879909df9d341a79d9d"
ZONE = "e234f8e868d45b7803f3823be7fbe2dd"
DOMAIN = "applevt.com"
TOKEN = "cfut_le4t4kUnydGPnbsAdF3WtK4qLOcTRjmbFqxM0Trtbbb1af3c"
HEADERS = {"Authorization": f"Bearer {TOKEN}",
           "Content-Type": "application/json"}
HOME = os.path.expanduser("~")
REPO = f"{HOME}/open-router"


def run(cmd, **kw): return subprocess.run(cmd, shell=True, **kw)
def log(msg): print(f"[+] {msg}", flush=True)


# ── PHASE 0: CLEANUP ─────────────────────────────────────────────────────────
log("Cleanup old tunnels + processes...")
run("pkill -f 'cloudflared|demo-test-minimax|supervisor.sh|cron-watchdog' 2>/dev/null; fuser -k 8787/tcp 2>/dev/null; true")

r = requests.get(f"{API}/accounts/{ACCOUNT}/cfd_tunnel", headers=HEADERS)
log(f"CF API status: {r.status_code}")
data = r.json() if r.status_code == 200 else {}
for t in (data.get("result") or []):
    requests.delete(
        f"{API}/accounts/{ACCOUNT}/cfd_tunnel/{t['id']}", headers=HEADERS)
    log(f"Deleted tunnel: {t['name']}")

dr = requests.get(f"{API}/zones/{ZONE}/dns_records",
                  headers=HEADERS, params={"type": "CNAME"})
for rec in (dr.json().get("result") or []):
    if "cfargotunnel.com" in rec.get("content", ""):
        requests.delete(
            f"{API}/zones/{ZONE}/dns_records/{rec['id']}", headers=HEADERS)
        log(f"Deleted DNS: {rec['name']}")

run("rm -rf ~/.cloudflared /tmp/tunnel-info.json /tmp/*.log")
time.sleep(1)

# ── PHASE 1: INSTALL cloudflared ─────────────────────────────────────────────
log("Installing cloudflared...")
run(f"mkdir -p {HOME}/bin && curl -sLo {HOME}/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x {HOME}/bin/cloudflared")
v = run(f"{HOME}/bin/cloudflared --version", capture_output=True, text=True)
log(f"cloudflared: {v.stdout.strip()}")

# ── PHASE 2: CLONE APP ───────────────────────────────────────────────────────
if not os.path.exists(f"{REPO}/demo-test-minimax.js"):
    log("Cloning repo...")
    run(f"cd {HOME} && rm -rf open-router && git clone https://github.com/phuong934223/open-router.git")
else:
    log("Repo exists, skipping clone")

# ── PHASE 3: CREATE TUNNEL + DNS + INGRESS (parallel) ────────────────────────
log("Creating tunnel...")
name = "vps-" + \
    ''.join(secrets.choice(string.ascii_lowercase + string.digits)
            for _ in range(8))
secret = secrets.token_hex(32)

r = requests.post(f"{API}/accounts/{ACCOUNT}/cfd_tunnel",
                  headers=HEADERS, json={"name": name, "tunnel_secret": secret}).json()
if not r.get("success"):
    print(f"ERROR creating tunnel: {json.dumps(r, indent=2)}")
    sys.exit(1)

tid = r["result"]["id"]
tname = r["result"]["name"]
atag = r["result"].get("account_tag", ACCOUNT)
ts = r["result"].get("credentials_file", {}).get("TunnelSecret") or secret
sub = ''.join(secrets.choice(string.ascii_lowercase + string.digits)
              for _ in range(10))

log(f"Tunnel: {tname} ({tid})")
log(f"Domain: {sub}.{DOMAIN}")


def create_dns():
    res = requests.post(f"{API}/zones/{ZONE}/dns_records", headers=HEADERS,
                        json={"type": "CNAME", "name": sub,
                              "content": f"{tid}.cfargotunnel.com", "proxied": True}).json()
    log(f"DNS CNAME: {'ok' if res.get('success') else res}")


def config_ingress():
    res = requests.put(f"{API}/accounts/{ACCOUNT}/cfd_tunnel/{tid}/configurations",
                       headers=HEADERS,
                       json={"config": {"ingress": [
                           {"hostname": f"{sub}.{DOMAIN}",
                               "service": "http://127.0.0.1:8787"},
                           {"service": "http_status:404"}
                       ]}}).json()
    log(f"Ingress config: {'ok' if res.get('success') else res}")


def write_creds():
    os.makedirs(f"{HOME}/.cloudflared", exist_ok=True)
    cpath = f"{HOME}/.cloudflared/{tid}.json"
    json.dump({"AccountTag": atag, "TunnelID": tid,
               "TunnelName": tname, "TunnelSecret": ts}, open(cpath, "w"), indent=2)
    json.dump({"tunnel_id": tid, "tunnel_name": tname, "tunnel_secret": ts,
               "subdomain": sub, "full_domain": f"{sub}.{DOMAIN}",
               "account_tag": atag}, open("/tmp/tunnel-info.json", "w"), indent=2)
    log(f"Credentials saved: {cpath}")


threads = [threading.Thread(target=f)
           for f in [create_dns, config_ingress, write_creds]]
for t in threads:
    t.start()
for t in threads:
    t.join()

# ── PHASE 4: WRITE supervisor.sh ─────────────────────────────────────────────
supervisor = f"""#!/bin/bash
LOG=/tmp/supervisor.log
TUNNEL_ID={tid}
log() {{ echo "[$(date '+%H:%M:%S')] $1" >> $LOG; }}
log "Supervisor started"

start_app() {{
    fuser -k 8787/tcp 2>/dev/null; sleep 1
    cd {REPO} && node demo-test-minimax.js >> /tmp/app-stdout.log 2>&1 &
    APP_PID=$!; log "App PID=$APP_PID"
}}

start_tunnel() {{
    {HOME}/bin/cloudflared tunnel --no-autoupdate run $TUNNEL_ID >> /tmp/tunnel-stdout.log 2>&1 &
    TUNNEL_PID=$!; log "Tunnel PID=$TUNNEL_PID"
}}

start_app; sleep 3; start_tunnel

while true; do
    sleep 15
    kill -0 $APP_PID 2>/dev/null || {{ log "APP DEAD - restart"; start_app; }}
    kill -0 $TUNNEL_PID 2>/dev/null || {{ log "TUNNEL DEAD - restart"; start_tunnel; }}
    curl -sf http://127.0.0.1:8787/health >/dev/null 2>&1 || {{ log "APP UNHEALTHY - restart"; kill $APP_PID 2>/dev/null; start_app; }}
    for f in /tmp/app-stdout.log /tmp/tunnel-stdout.log $LOG; do
        [ -f "$f" ] && [ $(stat -c%s "$f" 2>/dev/null || echo 0) -gt 5242880 ] && truncate -s 0 "$f"
    done
done
"""

# ── PHASE 5: WRITE cron-watchdog.sh ──────────────────────────────────────────
watchdog = f"""#!/bin/bash
LOG=/tmp/cron-watchdog.log
log() {{ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> $LOG; }}

APP_OK=$(curl -s -o /dev/null -w "%{{http_code}}" http://127.0.0.1:8787/health --max-time 3 2>/dev/null)
pgrep -f "cloudflared.*tunnel.*run" >/dev/null 2>&1 && TUNNEL_OK=1 || TUNNEL_OK=0
pgrep -f "supervisor.sh" >/dev/null 2>&1 && SUP_OK=1 || SUP_OK=0

[ "$APP_OK" = "200" ] && [ "$TUNNEL_OK" = "1" ] && [ "$SUP_OK" = "1" ] && exit 0

log "ISSUE: app=$APP_OK tunnel=$TUNNEL_OK supervisor=$SUP_OK"
TUNNEL_ID=$(python3 -c "import json; print(json.load(open('/tmp/tunnel-info.json'))['tunnel_id'])" 2>/dev/null)

[ "$APP_OK" != "200" ] && {{ fuser -k 8787/tcp 2>/dev/null; sleep 1; cd {REPO} && nohup node demo-test-minimax.js >> /tmp/app-stdout.log 2>&1 & sleep 3; log "App restarted"; }}
[ "$TUNNEL_OK" = "0" ] && [ -n "$TUNNEL_ID" ] && {{ nohup {HOME}/bin/cloudflared tunnel --no-autoupdate run $TUNNEL_ID >> /tmp/tunnel-stdout.log 2>&1 & log "Tunnel restarted"; }}
[ "$SUP_OK" = "0" ] && {{ nohup /bin/bash {REPO}/supervisor.sh >> /tmp/supervisor.log 2>&1 & log "Supervisor restarted"; }}

[ -f "$LOG" ] && [ $(stat -c%s "$LOG" 2>/dev/null || echo 0) -gt 5242880 ] && truncate -s 0 "$LOG"
"""

for path, content in [(f"{REPO}/supervisor.sh", supervisor), ("/tmp/cron-watchdog.sh", watchdog)]:
    open(path, "w").write(content)
    os.chmod(path, 0o755)
log("Scripts written")

# ── PHASE 6: DAEMONIZE supervisor ────────────────────────────────────────────
log("Daemonizing supervisor...")
daemonize = f"""
import os, sys
pid = os.fork()
if pid > 0: sys.exit(0)
os.setsid(); os.chdir("/")
pid2 = os.fork()
if pid2 > 0: sys.exit(0)
# Close all open fds trước khi dup2
import resource
maxfd = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
if maxfd == resource.RLIM_INFINITY: maxfd = 1024
for fd in range(3, maxfd): 
    try: os.close(fd)
    except: pass
null_fd = os.open("/dev/null", os.O_RDONLY)
log_fd  = os.open("/tmp/supervisor-daemon.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(null_fd, 0)
os.dup2(log_fd, 1)
os.dup2(log_fd, 2)
os.close(null_fd); os.close(log_fd)
os.execvp("/bin/bash", ["bash", "{REPO}/supervisor.sh"])
"""
subprocess.run([sys.executable, "-c", daemonize])
log("Supervisor daemonized")

# ── PHASE 7: BACKGROUND WATCHDOG LOOP (fallback thay crontab) ────────────────
log("Starting background watchdog loop...")
watchdog_loop = f"""
import subprocess, time, os
REPO  = "{REPO}"
HOME  = "{HOME}"
LOG   = "/tmp/bg-watchdog.log"

def log(m):
    with open(LOG, "a") as f: f.write(f"[bg-watchdog] {{m}}\\n")

while True:
    time.sleep(60)
    try:
        app = subprocess.run("curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:8787/health --max-time 3",
            shell=True, capture_output=True, text=True).stdout.strip()
        tunnel = subprocess.run("pgrep -f 'cloudflared.*tunnel.*run'",
            shell=True, capture_output=True).returncode == 0
        sup = subprocess.run("pgrep -f 'supervisor.sh'",
            shell=True, capture_output=True).returncode == 0

        if app == "200" and tunnel and sup:
            continue

        log(f"ISSUE: app={{app}} tunnel={{tunnel}} sup={{sup}}")

        if not sup:
            subprocess.Popen(["bash", f"{{REPO}}/supervisor.sh"],
                stdout=open("/tmp/supervisor.log","a"),
                stderr=subprocess.STDOUT,
                start_new_session=True)
            log("Supervisor restarted")
        elif app != "200":
            subprocess.run("fuser -k 8787/tcp 2>/dev/null; sleep 1", shell=True)
            subprocess.Popen(f"cd {{REPO}} && node demo-test-minimax.js >> /tmp/app-stdout.log 2>&1",
                shell=True, start_new_session=True)
            log("App restarted")
        if not tunnel:
            import json
            tid = json.load(open("/tmp/tunnel-info.json"))["tunnel_id"]
            subprocess.Popen(f"{{HOME}}/bin/cloudflared tunnel --no-autoupdate run {{tid}} >> /tmp/tunnel-stdout.log 2>&1",
                shell=True, start_new_session=True)
            log("Tunnel restarted")
    except Exception as e:
        log(f"ERROR: {{e}}")
"""
subprocess.Popen([sys.executable, "-c", watchdog_loop],
                 stdout=open("/tmp/bg-watchdog.log", "a"),
                 stderr=subprocess.STDOUT,
                 start_new_session=True)
log("Background watchdog started (60s interval)")

# ── PHASE 8: VERIFY ──────────────────────────────────────────────────────────
log("Waiting 8s for services to start...")
time.sleep(8)

app_health = run(
    "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/health --max-time 5", capture_output=True, text=True).stdout.strip()
tunnel_proc = run("pgrep -af 'cloudflared.*tunnel.*run'",
                  capture_output=True, text=True).stdout.strip()
sup_proc = run("pgrep -af 'supervisor.sh'",
               capture_output=True, text=True).stdout.strip()
wdog_proc = run("pgrep -af 'bg-watchdog'",
                capture_output=True, text=True).stdout.strip()

print(f"""
╔══════════════════════════════════════════════════════╗
║      TUNNEL DEPLOYED WITH ANTI-KILL PROTECTION      ║
╠══════════════════════════════════════════════════════╣
║  URL        : https://{sub}.{DOMAIN}
║  App        : {'✅ healthy' if app_health == '200' else f'❌ {app_health}'}
║  Tunnel     : {'✅ running' if tunnel_proc else '❌ not found'}
║  Supervisor : {'✅ running' if sup_proc else '❌ not found'}
║  Watchdog   : {'✅ running' if wdog_proc else '❌ not found'}
╠══════════════════════════════════════════════════════╣
║  Layer 1: Supervisor  — restart trong ≤15s          ║
║  Layer 2: Watchdog    — restart trong ≤60s          ║
║  Layer 3: Named Tunnel — auto-reconnect mạng        ║
╚══════════════════════════════════════════════════════╝
""")
