#!/usr/bin/env python3
import requests, json, secrets, string, os, subprocess, sys, time

API     = "https://api.cloudflare.com/client/v4"
ACCOUNT = "c975a170b2d36879909df9d341a79d9d"
ZONE    = "e234f8e868d45b7803f3823be7fbe2dd"
DOMAIN  = "applevt.com"
TOKEN   = os.environ.get("CF_API_TOKEN", "cfut_bNfi9EvyVDevaScwWMryn8BYZYUZ1dXfZMBdR8Ci21637983")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
HOME    = os.path.expanduser("~")
REPO    = f"{HOME}/open-router"

def run(cmd, **kw): return subprocess.run(cmd, shell=True, **kw)
def log(msg): print(f"[+] {msg}", flush=True)

# ── PHASE 0: CLEANUP (parallel) ─────────────────────────────────────────────
log("Cleanup old tunnels + processes...")

# Kill processes + cleanup CF in parallel
run("pkill -f 'cloudflared|demo-test-minimax|supervisor.sh|cron-watchdog' 2>/dev/null; fuser -k 8787/tcp 2>/dev/null; true")

# Delete all old tunnels + CNAME records
r = requests.get(f"{API}/accounts/{ACCOUNT}/cfd_tunnel", headers=HEADERS).json()
for t in r.get("result", []):
    requests.delete(f"{API}/accounts/{ACCOUNT}/cfd_tunnel/{t['id']}", headers=HEADERS)
    log(f"Deleted tunnel: {t['name']}")

dr = requests.get(f"{API}/zones/{ZONE}/dns_records", headers=HEADERS, params={"type": "CNAME"}).json()
for rec in dr.get("result", []):
    if "cfargotunnel.com" in rec.get("content", ""):
        requests.delete(f"{API}/zones/{ZONE}/dns_records/{rec['id']}", headers=HEADERS)
        log(f"Deleted DNS: {rec['name']}")

run(f"rm -rf ~/.cloudflared /tmp/tunnel-info.json /tmp/*.log")
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

# ── PHASE 3: CREATE TUNNEL + DNS + INGRESS (parallel API calls) ─────────────
log("Creating tunnel...")
name = "vps-" + ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
secret = secrets.token_hex(32)

r = requests.post(f"{API}/accounts/{ACCOUNT}/cfd_tunnel",
    headers=HEADERS, json={"name": name, "tunnel_secret": secret}).json()
if not r.get("success"):
    print(f"ERROR: {json.dumps(r, indent=2)}"); sys.exit(1)

tid   = r["result"]["id"]
tname = r["result"]["name"]
atag  = r["result"].get("account_tag", ACCOUNT)
ts    = r["result"].get("credentials_file", {}).get("TunnelSecret") or secret
sub   = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(10))

log(f"Tunnel: {tname} ({tid})")
log(f"Domain: {sub}.{DOMAIN}")

# DNS + ingress config in parallel via threads
import threading

def create_dns():
    requests.post(f"{API}/zones/{ZONE}/dns_records", headers=HEADERS,
        json={"type": "CNAME", "name": sub, "content": f"{tid}.cfargotunnel.com",
              "proxied": True}).json()
    log("DNS CNAME created")

def config_ingress():
    requests.put(f"{API}/accounts/{ACCOUNT}/cfd_tunnel/{tid}/configurations",
        headers=HEADERS,
        json={"config": {"ingress": [
            {"hostname": f"{sub}.{DOMAIN}", "service": "http://127.0.0.1:8787"},
            {"service": "http_status:404"}
        ]}}).json()
    log("Ingress configured")

def write_creds():
    os.makedirs(f"{HOME}/.cloudflared", exist_ok=True)
    cpath = f"{HOME}/.cloudflared/{tid}.json"
    json.dump({"AccountTag": atag, "TunnelID": tid, "TunnelName": tname, "TunnelSecret": ts},
              open(cpath, "w"), indent=2)
    json.dump({"tunnel_id": tid, "tunnel_name": tname, "tunnel_secret": ts,
               "subdomain": sub, "full_domain": f"{sub}.{DOMAIN}", "account_tag": atag},
              open("/tmp/tunnel-info.json", "w"), indent=2)
    log(f"Credentials saved: {cpath}")

threads = [threading.Thread(target=f) for f in [create_dns, config_ingress, write_creds]]
for t in threads: t.start()
for t in threads: t.join()

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
        [ -f "$f" ] && [ $(stat -c%s "$f" 2>/dev/null||echo 0) -gt 5242880 ] && truncate -s 0 "$f"
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

[ -f "$LOG" ] && [ $(stat -c%s "$LOG" 2>/dev/null||echo 0) -gt 5242880 ] && truncate -s 0 "$LOG"
"""

for path, content in [(f"{REPO}/supervisor.sh", supervisor), ("/tmp/cron-watchdog.sh", watchdog)]:
    open(path, "w").write(content)
    os.chmod(path, 0o755)
log("Scripts written")

# ── PHASE 6: DAEMONIZE supervisor ────────────────────────────────────────────
log("Daemonizing supervisor...")
pid = os.fork()
if pid > 0:
    os.waitpid(pid, 0)
else:
    os.setsid(); os.chdir("/")
    if os.fork() > 0: sys.exit(0)
    sys.stdout.flush(); sys.stderr.flush()
    os.dup2(open("/dev/null","r").fileno(), 0)
    os.dup2(open("/tmp/supervisor-daemon.log","a").fileno(), 1)
    os.dup2(open("/tmp/supervisor-daemon.log","a").fileno(), 2)
    os.execvp("/bin/bash", ["bash", f"{REPO}/supervisor.sh"])

# ── PHASE 7: INSTALL CRON ────────────────────────────────────────────────────
log("Installing cron watchdog...")
run("sudo service cron start 2>/dev/null || true")
existing = subprocess.run(["crontab","-l"], capture_output=True, text=True)
current = existing.stdout if existing.returncode == 0 else ""
if "cron-watchdog" not in current:
    subprocess.run(["crontab","-"], input=current + "* * * * * /tmp/cron-watchdog.sh\n", text=True)

# ── PHASE 8: VERIFY ──────────────────────────────────────────────────────────
log("Waiting 8s for services to start...")
time.sleep(8)

app_health = run("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/health --max-time 5", capture_output=True, text=True).stdout
tunnel_proc = run("pgrep -af 'cloudflared.*tunnel.*run'", capture_output=True, text=True).stdout.strip()
sup_proc    = run("pgrep -af 'supervisor.sh'", capture_output=True, text=True).stdout.strip()
cron_list   = run("crontab -l", capture_output=True, text=True).stdout.strip()

print(f"""
╔══════════════════════════════════════════════════════╗
║       TUNNEL DEPLOYED WITH ANTI-KILL PROTECTION     ║
╠══════════════════════════════════════════════════════╣
║  URL  : https://{sub}.{DOMAIN}
║  App  : {'✅ healthy' if app_health == '200' else f'❌ {app_health}'}
║  Tunnel: {'✅ running' if tunnel_proc else '❌ not found'}
║  Supervisor: {'✅ running' if sup_proc else '❌ not found'}
║  Cron: {'✅ installed' if 'cron-watchdog' in cron_list else '❌ not installed'}
╠══════════════════════════════════════════════════════╣
║  Layer 1: Supervisor   — restart trong ≤15s         ║
║  Layer 2: Cron watchdog — restart trong ≤60s        ║
║  Layer 3: Named Tunnel  — auto-reconnect mạng       ║
╚══════════════════════════════════════════════════════╝
""")