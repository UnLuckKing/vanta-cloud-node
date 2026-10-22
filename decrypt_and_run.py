import os, sys, subprocess, base64, hashlib, tarfile, time, re

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
    
    # 3. Setup environment variables for orchestrator
    env = os.environ.copy()
    env["PYTHONPATH"] = vanta_home
    env["VANTA_ORCH_BIND"] = "0.0.0.0"
    env["VANTA_ENTITLEMENT_PUBLIC_KEYS"] = "primary:spc4UHKE1AVdqc88A1lQhSLEFeWNAbpBW34039ubiV0"
    env["VANTA_ORCHESTRATOR_ADMIN_TOKEN"] = "y_KQr0l6KGCYcm9KeJWDKW6IS8RYqwNAdK2UDBp3FghfdykhAykgKbskIYbcbohn"
    env["VANTA_PUBLIC_HOST"] = "127.0.0.1"
    
    # 4. Launch orchestrator as background process
    orch_path = os.path.join(vanta_home, "vanta_orchestrator.py")
    orch_log = open("orchestrator.log", "w")
    print(f"Launching orchestrator from {orch_path}...")
    orch_proc = subprocess.Popen([sys.executable, "-u", orch_path], env=env, stdout=orch_log, stderr=subprocess.STDOUT)
    
    # 5. Start Pinggy TCP Tunnel on port 9000
    os.makedirs("tunnel_logs", exist_ok=True)
    tunnel_cmd = "ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=10 -p 443 -R0:localhost:9000 tcp@a.pinggy.io > tunnel_logs/pinggy.log 2>&1"
    tunnel_proc = subprocess.Popen(tunnel_cmd, shell=True)
    
    tunnel_addr = ""
    for attempt in range(30):
        time.sleep(1)
        if os.path.exists("tunnel_logs/pinggy.log"):
            with open("tunnel_logs/pinggy.log", "r") as log:
                c = log.read()
                m = re.findall(r'([a-z0-9]+\.a\.pinggy\.io:\d+|a\.pinggy\.io:\d+)', c)
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
            
    # Write live endpoint to a committed or accessible state
    with open("live_endpoint.txt", "w") as ef:
        ef.write(tunnel_addr)
        
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
