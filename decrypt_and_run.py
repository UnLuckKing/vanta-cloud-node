import os, sys, subprocess, base64, hashlib, tarfile, time, re

def git_commit_and_push(file_path, message):
    try:
        subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", file_path], check=True)
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "push"], check=True)
        print("Successfully published status to repository branch!")
    except Exception as e:
        print("Git publish error (non-fatal):", e)

def main():
    print("=== VANTA APPLE SILICON RUNNER INITIALIZING ===")
    
    # 1. Install dependencies
    subprocess.run([sys.executable, "-m", "pip", "install", "cryptography", "paramiko", "requests", "pyyaml"], check=True)
    from cryptography.fernet import Fernet
    
    # 2. Decrypt bundle
    key = base64.urlsafe_b64encode(hashlib.sha256(b"vanta_apex_root_2026!").digest())
    f = Fernet(key)
    
    with open("m1_bundle.enc", "rb") as fp:
        raw = f.decrypt(fp.read())
        
    vanta_home = os.path.expanduser("~/.vanta")
    slots_home = os.path.expanduser("~/VantaSlots")
    os.makedirs(vanta_home, exist_ok=True)
    os.makedirs(slots_home, exist_ok=True)
    
    tar_path = "/tmp/m1_bundle.tar.gz"
    with open(tar_path, "wb") as fp:
        fp.write(raw)
        
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(path=vanta_home)
        
    print("Payload successfully decrypted & unpacked to ~/.vanta")
    
    auth_dir = os.path.join(vanta_home, "vanta-auth")
    os.makedirs(os.path.join(auth_dir, "secrets"), exist_ok=True)
    os.makedirs(os.path.join(auth_dir, "data"), exist_ok=True)
    
    # 3. Setup environment variables for orchestrator and auth
    env = os.environ.copy()
    env["PYTHONPATH"] = vanta_home
    env["VANTA_ORCH_BIND"] = "0.0.0.0"
    env["VANTA_ENTITLEMENT_PUBLIC_KEYS"] = "primary:spc4UHKE1AVdqc88A1lQhSLEFeWNAbpBW34039ubiV0"
    env["VANTA_ORCHESTRATOR_ADMIN_TOKEN"] = "y_KQr0l6KGCYcm9KeJWDKW6IS8RYqwNAdK2UDBp3FghfdykhAykgKbskIYbcbohn"
    env["VANTA_PUBLIC_HOST"] = "127.0.0.1"
    
    pem_path = os.path.join(auth_dir, "secrets", "primary.private.pem")
    db_path = os.path.join(auth_dir, "data", "auth.sqlite3")
    env["VANTA_SIGNING_PRIVATE_KEY"] = pem_path
    env["VANTA_AUTH_DB"] = db_path
    
    # 4. Patch orchestrator to embed AuthHandler for /api/client/* endpoints
    orch_path = os.path.join(vanta_home, "vanta_orchestrator.py")
    with open(orch_path, "r", encoding="utf-8", errors="ignore") as opf:
        orch_code = opf.read()
        
    patch_block = '''
# --- VANTA INTEGRATED AUTH SERVER ---
try:
    import sys, os
    sys.path.insert(0, os.path.expanduser("~/.vanta/vanta-auth"))
    from app.server.auth import server as auth_srv
    auth_srv.PRIVATE_KEY_PATH = os.environ.get("VANTA_SIGNING_PRIVATE_KEY", "")
    auth_srv.DB_PATH = os.environ.get("VANTA_AUTH_DB", "")
    auth_srv.store = auth_srv.AuthStore(auth_srv.DB_PATH, auth_srv.ISSUER)
    auth_srv._load_private()
    print("[auth] Integrated Auth Engine successfully mounted into Orchestrator!")
except Exception as auth_err:
    print(f"[auth] Integrated Auth Engine mount notice: {auth_err}")
# -----------------------------------
'''
    if "Integrated Auth Engine" not in orch_code:
        orch_code = patch_block + orch_code
        get_intercept = '''    def do_GET(self):
        if self.path.startswith("/api/client/challenge") or self.path.startswith("/admin"):
            try:
                auth_srv.Handler.do_GET(self)
                return
            except Exception as e:
                print(f"[auth] GET dispatch err: {e}")
'''
        post_intercept = '''    def do_POST(self):
        if self.path.startswith("/api/client/auth") or self.path.startswith("/api/client/refresh") or self.path.startswith("/admin"):
            try:
                auth_srv.Handler.do_POST(self)
                return
            except Exception as e:
                print(f"[auth] POST dispatch err: {e}")
'''
        orch_code = orch_code.replace("    def do_GET(self):", get_intercept, 1)
        orch_code = orch_code.replace("    def do_POST(self):", post_intercept, 1)
        with open(orch_path, "w", encoding="utf-8") as opf:
            opf.write(orch_code)
        print("Orchestrator successfully unified with auth engine!")

    orch_log = open("orchestrator.log", "w")
    print(f"Launching orchestrator from {orch_path}...")
    orch_proc = subprocess.Popen([sys.executable, "-u", orch_path], env=env, stdout=orch_log, stderr=subprocess.STDOUT)
    
    # 5. Start Pinggy TCP Tunnel on port 9000
    os.makedirs("tunnel_logs", exist_ok=True)
    log_file = "tunnel_logs/pinggy.log"
    tunnel_cmd = f"ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=10 -p 443 -R0:localhost:9000 tcp@a.pinggy.io > {log_file} 2>&1"
    tunnel_proc = subprocess.Popen(tunnel_cmd, shell=True)
    
    tunnel_addr = ""
    for attempt in range(40):
        time.sleep(1)
        if os.path.exists(log_file):
            with open(log_file, "r") as log:
                c = log.read()
                m = re.findall(r'(?:tcp://)?([a-zA-Z0-9.\-_]+\.(?:pinggy\.io|pinggy-free\.link):\d+)', c)
                if m:
                    tunnel_addr = m[0]
                    break
        if orch_proc.poll() is not None:
            print("ERROR: Orchestrator died early! Checking log:")
            with open("orchestrator.log", "r") as ol:
                print(ol.read())
            break
            
    print("==================================================")
    print("VANTA LIVE M1 ORCHESTRATOR URL:", tunnel_addr)
    print("==================================================")
    
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as sf:
            sf.write(f"\n## 🚀 VANTA Apple Silicon Node Online!\n**Live Endpoint:** `{tunnel_addr}`\n")
            
    with open("live_endpoint.txt", "w") as ef:
        ef.write(tunnel_addr + "\n")
        
    git_commit_and_push("live_endpoint.txt", f"Publish live node endpoint: {tunnel_addr}")
        
    # 6. Keep-alive monitor loop
    print("Orchestrator & Tunnel running. Entering monitor loop...")
    try:
        while True:
            time.sleep(10)
            if orch_proc.poll() is not None:
                print("Orchestrator process exited with code", orch_proc.returncode)
                with open("orchestrator.log", "r") as ol:
                    print(ol.read())
                break
            if tunnel_proc.poll() is not None:
                print("Tunnel disconnected, attempting restart...")
                tunnel_proc = subprocess.Popen(tunnel_cmd, shell=True)
    except KeyboardInterrupt:
        print("Terminating...")
    finally:
        orch_proc.terminate()
        tunnel_proc.terminate()

if __name__ == "__main__":
    main()
