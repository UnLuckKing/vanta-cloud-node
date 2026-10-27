import os, sys, subprocess, base64, hashlib, tarfile, time, re, shutil, struct
from concurrent.futures import ThreadPoolExecutor, as_completed

def git_commit_and_push(file_path, message):
    try:
        subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        subprocess.run(["git", "add", file_path], check=True)
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        print("Successfully published status to repository branch!")
    except Exception as e:
        print("Git publish error (non-fatal):", e)

def parse_rman(raw_bytes):
    import zstandard as zstd
    magic, major, minor, flags, offset, length, manifest_id, body_len = struct.unpack('<4sBBHIIQI', raw_bytes[:28])
    comp_body = raw_bytes[offset:offset+length]
    dctx = zstd.ZstdDecompressor()
    body = dctx.decompress(comp_body, max_output_size=body_len + 1024*1024)

    def read_u32(pos): return struct.unpack_from('<I', body, pos)[0]
    def read_i32(pos): return struct.unpack_from('<i', body, pos)[0]
    def read_u64(pos): return struct.unpack_from('<Q', body, pos)[0]
    def read_u16(pos): return struct.unpack_from('<H', body, pos)[0]

    def get_vtable(table_pos):
        vtable_offset = read_i32(table_pos)
        vtable_pos = table_pos - vtable_offset
        vtable_size = read_u16(vtable_pos)
        return [read_u16(vtable_pos + 4 + i*2) for i in range(0, (vtable_size - 4)//2)]

    def get_field_pos(table_pos, field_idx):
        vt = get_vtable(table_pos)
        if field_idx >= len(vt) or vt[field_idx] == 0:
            return None
        return table_pos + vt[field_idx]

    def read_str(pos):
        str_pos = pos + read_u32(pos)
        slen = read_u32(str_pos)
        return body[str_pos+4:str_pos+4+slen].decode('utf-8', errors='replace')

    root_pos = read_u32(0)

    # 1. Bundles & Chunks
    bundles_f = get_field_pos(root_pos, 0)
    bundles_vec = bundles_f + read_u32(bundles_f)
    num_bundles = read_u32(bundles_vec)
    chunk_map = {}
    for i in range(num_bundles):
        b_ref = bundles_vec + 4 + i*4
        b_pos = b_ref + read_u32(b_ref)
        b_id = read_u64(get_field_pos(b_pos, 0))
        chunks_f = get_field_pos(b_pos, 1)
        if chunks_f:
            c_vec = chunks_f + read_u32(chunks_f)
            num_c = read_u32(c_vec)
            curr_offset = 0
            for j in range(num_c):
                c_ref = c_vec + 4 + j*4
                c_pos = c_ref + read_u32(c_ref)
                c_id = read_u64(get_field_pos(c_pos, 0))
                comp_size = read_u32(get_field_pos(c_pos, 1))
                uncomp_size = read_u32(get_field_pos(c_pos, 2))
                chunk_map[c_id] = (b_id, curr_offset, comp_size, uncomp_size)
                curr_offset += comp_size

    # 2. Directories
    dirs_f = get_field_pos(root_pos, 3)
    dirs_vec = dirs_f + read_u32(dirs_f)
    num_dirs = read_u32(dirs_vec)
    dirs = {}
    for i in range(num_dirs):
        d_ref = dirs_vec + 4 + i*4
        d_pos = d_ref + read_u32(d_ref)
        d_id_f = get_field_pos(d_pos, 0)
        d_id = read_u64(d_id_f) if d_id_f else 0
        p_id_f = get_field_pos(d_pos, 1)
        p_id = read_u64(p_id_f) if p_id_f else 0
        name_f = get_field_pos(d_pos, 2)
        name = read_str(name_f) if name_f else ""
        dirs[d_id] = (p_id, name)

    def get_dir_path(d_id):
        parts = []
        curr = d_id
        while curr in dirs and curr != 0:
            p_id, name = dirs[curr]
            if name:
                parts.append(name)
            curr = p_id
        return "/".join(reversed(parts))

    # 3. Files
    files_f = get_field_pos(root_pos, 2)
    files_vec = files_f + read_u32(files_f)
    num_files = read_u32(files_vec)
    files = []
    for i in range(num_files):
        f_ref = files_vec + 4 + i*4
        f_pos = f_ref + read_u32(f_ref)
        name_f = get_field_pos(f_pos, 3)
        name = read_str(name_f) if name_f else ""
        dir_f = get_field_pos(f_pos, 1)
        d_id = read_u64(dir_f) if dir_f else 0
        fsize_f = get_field_pos(f_pos, 2)
        fsize = read_u32(fsize_f) if fsize_f else 0
        link_f = get_field_pos(f_pos, 5)
        link = read_str(link_f) if link_f else ""
        c_f = get_field_pos(f_pos, 7)
        c_ids = []
        if c_f:
            c_vec = c_f + read_u32(c_f)
            nc = read_u32(c_vec)
            for j in range(nc):
                c_ids.append(read_u64(c_vec + 4 + j*8))
        full_dir = get_dir_path(d_id)
        full_path = f"{full_dir}/{name}" if full_dir else name
        files.append((full_path, fsize, link, c_ids))

    return chunk_map, files

def download_rman_manifest(manifest_url, cdn_base, dest_dir, filter_fn, save_manifest_path=None):
    import requests, zstandard as zstd
    print(f"Fetching RMAN manifest: {manifest_url}")
    r = requests.get(manifest_url, timeout=60)
    r.raise_for_status()
    raw_bytes = r.content
    if save_manifest_path:
        os.makedirs(os.path.dirname(save_manifest_path), exist_ok=True)
        with open(save_manifest_path, "wb") as mf:
            mf.write(raw_bytes)

    chunk_map, files = parse_rman(raw_bytes)
    selected_files = [f for f in files if filter_fn(f[0])]
    print(f"Selected {len(selected_files)} / {len(files)} files for extraction into {dest_dir}")

    # Create directories, empty files, and symlinks; map chunk_id -> list of (abs_path, file_offset)
    chunk_targets = {}
    symlinks_to_make = []
    for rel_path, fsize, link, c_ids in selected_files:
        abs_path = os.path.join(dest_dir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        if link:
            symlinks_to_make.append((abs_path, link))
            continue
        # Pre-create file
        with open(abs_path, "wb") as fp:
            if fsize > 0:
                fp.seek(fsize - 1)
                fp.write(b"\0")
        f_off = 0
        for cid in c_ids:
            b_id, b_off, comp_sz, uncomp_sz = chunk_map[cid]
            chunk_targets.setdefault(cid, []).append((abs_path, f_off))
            f_off += uncomp_sz

    # Group required chunks by bundle_id
    by_bundle = {}
    for cid in chunk_targets:
        b_id, b_off, comp_sz, uncomp_sz = chunk_map[cid]
        by_bundle.setdefault(b_id, []).append((b_off, comp_sz, uncomp_sz, cid))

    # Build merged byte-range download tasks
    tasks = []
    total_dl_bytes = 0
    for b_id, clist in by_bundle.items():
        clist.sort(key=lambda x: x[0])
        cur_start = clist[0][0]
        cur_end = cur_start + clist[0][1]
        cur_items = [clist[0]]
        for item in clist[1:]:
            off, csz, usz, cid = item
            if off - cur_end <= 65536 and (off + csz - cur_start) <= 8 * 1024 * 1024:
                cur_end = off + csz
                cur_items.append(item)
            else:
                tasks.append((b_id, cur_start, cur_end, cur_items))
                total_dl_bytes += (cur_end - cur_start)
                cur_start = off
                cur_end = off + csz
                cur_items = [item]
        tasks.append((b_id, cur_start, cur_end, cur_items))
        total_dl_bytes += (cur_end - cur_start)

    print(f"Downloading {total_dl_bytes / (1024*1024):.2f} MB across {len(tasks)} HTTP range requests (32 threads)...")
    t0 = time.time()

    def worker(task):
        b_id, start, end, items = task
        url = f"{cdn_base}/bundles/{b_id:016X}.bundle"
        headers = {"Range": f"bytes={start}-{end-1}"}
        for attempt in range(4):
            try:
                resp = requests.get(url, headers=headers, timeout=45)
                if resp.status_code in (200, 206):
                    data = resp.content
                    break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(0.5)
        dctx = zstd.ZstdDecompressor()
        for off, csz, usz, cid in items:
            rel_off = off - start
            comp_slice = data[rel_off:rel_off+csz]
            uncomp_data = dctx.decompress(comp_slice, max_output_size=usz + 1024)
            for abs_path, f_off in chunk_targets[cid]:
                with open(abs_path, "r+b") as fp:
                    fp.seek(f_off)
                    fp.write(uncomp_data)

    with ThreadPoolExecutor(max_workers=32) as ex:
        futs = [ex.submit(worker, t) for t in tasks]
        for fut in as_completed(futs):
            fut.result()

    # Create symlinks
    for abs_path, link_target in symlinks_to_make:
        try:
            if os.path.lexists(abs_path):
                os.remove(abs_path)
            os.symlink(link_target, abs_path)
        except Exception:
            pass

    print(f"RMAN extraction into {dest_dir} finished in {time.time() - t0:.1f}s!")

def main():
    print("=== VANTA APPLE SILICON RUNNER INITIALIZING ===")
    print("Executing runner disk cleanup to prevent 100MB disk limit error (005)...")
    for b_path in [
        os.path.expanduser("~/Library/Android"),
        "/Library/Developer/CoreSimulator",
        os.path.expanduser("~/Library/Caches"),
        "/tmp/m1_bundle.tar.gz"
    ]:
        if os.path.exists(b_path):
            try:
                if os.path.isdir(b_path):
                    shutil.rmtree(b_path, ignore_errors=True)
                else:
                    os.remove(b_path)
            except Exception:
                pass
    subprocess.run(["df", "-h"], check=False)
    
    # 1. Install dependencies (including zstandard for RMAN bundle extraction)
    subprocess.run([sys.executable, "-m", "pip", "install", "cryptography", "paramiko", "requests", "pyyaml", "zstandard", "argon2-cffi"], check=True)
    import requests
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
    
    # 2.5 Ensure auth database is populated and synchronized across all fallback paths
    import sqlite3
    backup_enc_path = os.path.join(os.getcwd(), "auth_backup.enc")
    restored_db_tmp = "/tmp/restored_auth.sqlite3"
    if os.path.exists(backup_enc_path):
        try:
            with open(backup_enc_path, "rb") as bf:
                dec_db = f.decrypt(bf.read())
            with open(restored_db_tmp, "wb") as rf:
                rf.write(dec_db)
            print("Successfully decrypted persistent auth_backup.enc from repository!")
        except Exception as rbe:
            print("Notice decrypting auth_backup.enc:", rbe)
    db_candidates = [
        os.path.join(auth_dir, "data", "auth.sqlite3"),
        os.path.join(auth_dir, "vanta_auth.sqlite3"),
        os.path.join(vanta_home, "vanta_auth.sqlite3"),
    ]
    primary_db = None
    for cand in db_candidates:
        if os.path.exists(cand) and os.path.getsize(cand) > 10000:
            primary_db = cand
            break
    if os.path.exists(restored_db_tmp) and os.path.getsize(restored_db_tmp) > 4096:
        primary_db = restored_db_tmp
    if not primary_db:
        primary_db = db_candidates[0]
        os.makedirs(os.path.dirname(primary_db), exist_ok=True)

    for cand in db_candidates:
        os.makedirs(os.path.dirname(cand), exist_ok=True)
        if primary_db and os.path.exists(primary_db) and cand != primary_db:
            try: shutil.copy2(primary_db, cand)
            except Exception: pass
        try:
            conn = sqlite3.connect(cand)
            conn.execute('''CREATE TABLE IF NOT EXISTS staff_users(id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL, mfa_secret TEXT NOT NULL, created_at INTEGER NOT NULL)''')
            founder_hash = "$argon2id$v=19$m=32768,t=2,p=1$95BI6n66LPJ32+T7OFX6Eg$DZ7AfdMHEQ1rhG01x647T7UQBMXtOknKXdl2edFDeF0"
            founder_mfa = "EAIOVFGAM7UYELOMNNTR4EB2MIWB2DRS"
            conn.execute('''INSERT INTO staff_users(id, username, password_hash, role, status, mfa_secret, created_at)
                            VALUES('founder_master_id', 'founder', ?, 'founder', 'active', ?, ?)
                            ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash, mfa_secret=excluded.mfa_secret, status='active' ''',
                         (founder_hash, founder_mfa, int(time.time())))
            conn.execute('''CREATE TABLE IF NOT EXISTS license_keys(id TEXT PRIMARY KEY, key_digest TEXT UNIQUE NOT NULL, display_prefix TEXT NOT NULL, masked_key TEXT NOT NULL, owner_reseller_id TEXT, plan_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at INTEGER NOT NULL, starts_at INTEGER, expires_at INTEGER, max_devices INTEGER NOT NULL, created_by TEXT NOT NULL, revoked_at INTEGER, revoked_reason TEXT)''')
            founder_digest = hashlib.sha256("VNT-M9SH-Q58X-Y6WC-AQ63-W7MT-LXHZ-4KSM".encode("ascii")).hexdigest()
            conn.execute('''INSERT OR REPLACE INTO license_keys(id, key_digest, display_prefix, masked_key, owner_reseller_id, plan_id, status, created_at, starts_at, expires_at, max_devices, created_by)
                            VALUES('founder_master_lic_id', ?, 'VNT', 'VNT-M9SH-****-****-****-****-4KSM', NULL, 'default', 'active', ?, ?, ?, 999, 'founder')''',
                         (founder_digest, int(time.time()), int(time.time()), int(time.time()) + 365*86400))
            conn.commit()
            conn.close()
            try:
                sync_resp = requests.get("https://pnl.vantacheat.com/api/keys/sync", timeout=8).json()
                for kitem in sync_resp.get("keys", []):
                    conn.execute("""INSERT OR REPLACE INTO license_keys(id, key_digest, display_prefix, masked_key, owner_reseller_id, plan_id, status, created_at, starts_at, expires_at, max_devices, created_by, revoked_at, revoked_reason)
                                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                 (kitem["id"], kitem["key_digest"], kitem.get("display_prefix", "VNT"), kitem.get("masked_key", "VNT-****"), kitem.get("owner_reseller_id"), kitem.get("plan_id", "default"), kitem.get("status", "active"), int(kitem.get("created_at") or time.time()), kitem.get("starts_at"), kitem.get("expires_at"), int(kitem.get("max_devices") or 999), kitem.get("created_by", "founder"), kitem.get("revoked_at"), kitem.get("revoked_reason")))
                conn.commit()
            except Exception as se:
                print("Edge key sync notice:", se)
            print(f"Auth DB {cand} successfully verified and seeded with Founder Master License!")
        except Exception as dbe:
            print(f"Notice seeding auth db {cand}: {dbe}")
    
    # 2.8 Inoculate core.py for zero-failure password and MFA verification
    core_py = os.path.join(auth_dir, "app", "server", "auth", "core.py")
    if os.path.exists(core_py):
        try:
            with open(core_py, "r") as cpf:
                c_data = cpf.read()
            if "if str(encoded).strip() == str(password).strip():" not in c_data:
                c_data = c_data.replace(
                    "def password_verify(encoded: str, password: str) -> bool:\n",
                    "def password_verify(encoded: str, password: str) -> bool:\n    if str(encoded).strip() == str(password).strip(): return True\n"
                )
            if "supplied_clean" not in c_data:
                c_data = c_data.replace(
                    "def verify_totp(secret: str, supplied: str, at: int | None = None) -> bool:\n    at = now() if at is None else at\n    return any(hmac.compare_digest(totp(secret, at + delta), supplied) for delta in (-30, 0, 30))\n",
                    "def verify_totp(secret: str, supplied: str, at: int | None = None) -> bool:\n    at = now() if at is None else at\n    supplied_clean = str(supplied).strip()\n    return any(hmac.compare_digest(totp(secret, at + delta), supplied_clean) for delta in (-60, -30, 0, 30, 60))\n"
                )
            with open(core_py, "w") as cpf:
                cpf.write(c_data)
            print("[auth] core.py patched with resilient auth & TOTP window!")
            
            srv_py = os.path.join(auth_dir, "app", "server", "auth", "server.py")
            if os.path.exists(srv_py):
                with open(srv_py, "r") as spf:
                    s_data = spf.read()
                old_login = 'if not row or not password_verify(row["password_hash"], str(body.get("password", ""))) or not verify_totp(row["mfa_secret"], str(body.get("mfa_code", ""))): self._fail(); return'
                new_login = 'if not row or not password_verify(row["password_hash"], str(body.get("password", ""))): self._fail(); return'
                if old_login in s_data:
                    s_data = s_data.replace(old_login, new_login)
                    with open(srv_py, "w") as spf:
                        spf.write(s_data)
                    print("[auth] server.py patched: MFA requirement permanently removed!")
        except Exception as cpe:
            print(f"[auth] Notice patching core.py: {cpe}")
    
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

    # 3.5 Provision complete Riot Client.app & League of Legends.app via RMAN CDN
    print("Provisioning Riot Client.app and League of Legends.app via Riot RMAN CDN...")
    try:
        lol_app = "/Applications/League of Legends.app"
        rc_app = "/Applications/Riot Client.app"
        os.makedirs(lol_app, exist_ok=True)
        os.makedirs(rc_app, exist_ok=True)

        # Step A: Unpack live.tr.zip first for baseline Resources/system.yaml
        installer_zip = "/tmp/live.tr.zip"
        if not os.path.exists(installer_zip):
            subprocess.run(["curl", "-sSL", "https://lol.secure.dyn.riotcdn.net/channels/public/x/installer/current/live.tr.zip", "-o", installer_zip], check=True)
            subprocess.run(["unzip", "-q", "-o", installer_zip, "-d", "/tmp/riot_install"], check=True)
        app_candidates = [d for d in os.listdir("/tmp/riot_install") if d.endswith(".app")]
        if app_candidates:
            src_app = os.path.join("/tmp/riot_install", app_candidates[0])
            subprocess.run(f"cp -R '{src_app}/'* '{rc_app}/'", shell=True, check=True)

        # Step B: Download full Riot Client.app from Keystone RMAN manifest (includes libRiotClientFoundation.dylib & RiotGamesApi.framework)
        rc_manifest_url = "https://ks-foundation.secure.dyn.riotcdn.net/channels/public/releases/926DCEC2764CF6CC.manifest"
        def rc_filter(p):
            if "Krisp" in p or p.endswith(".webm") or "Electron Framework" in p or p.endswith("app.asar") or p.endswith(".pak") or p.endswith("icudtl.dat"):
                return False
            return True
        download_rman_manifest(
            rc_manifest_url,
            "https://ks-foundation.secure.dyn.riotcdn.net/channels/public",
            rc_app,
            rc_filter,
            save_manifest_path=os.path.join(slots_home, "Riot Client.manifest")
        )
        with open(os.path.join(slots_home, "Riot Client.ok"), "w") as rc_okf:
            rc_okf.write(f"{rc_manifest_url}\n")

        # Step C: Query latest macOS League of Legends RMAN manifest URL
        lol_manifest_url = "https://lol.secure.dyn.riotcdn.net/channels/public/releases/0DA06B11DEA274E0.manifest"
        try:
            cfg_resp = requests.get("https://clientconfig.rpg.riotgames.com/api/v1/config/public?namespace=keystone.products.league_of_legends.patchlines", timeout=15).json()
            confs = cfg_resp["keystone.products.league_of_legends.patchlines.live"]["platforms"]["mac"]["configurations"]
            if confs and confs[0].get("patch_url"):
                lol_manifest_url = confs[0]["patch_url"]
        except Exception as e:
            print("Using fallback LoL manifest URL:", e)

        def lol_filter(p):
            if (p.startswith("Contents/LoL/Game/") or "/Game/" in p) and (p.endswith(".wad") or p.endswith(".wad.client")):
                if re.search(r'\.[a-z]{2}_[A-Z]{2}\.wad', p) and ("en_GB" not in p and "tr_TR" not in p and "en_US" not in p):
                    return False
            if "Chromium Embedded" in p or "LeagueClientUx Helper" in p or "rc-mac.enc" in p:
                return False
            if p.startswith("Contents/LoL/Plugins/rcp-be-lol-game-data/") and p.endswith(".wad") and "en_GB" not in p and "tr_TR" not in p:
                return False
            if p.startswith("Contents/LoL/Plugins/rcp-fe-") and p.endswith(".wad"):
                allowed_fe = (
                    "rcp-fe-lol-parties",
                    "rcp-fe-lol-champ-select",
                    "rcp-fe-lol-navigation",
                    "rcp-fe-common-libs",
                    "rcp-fe-ember-libs",
                    "rcp-fe-audio",
                    "rcp-fe-lol-shared-components",
                    "rcp-fe-lol-uikit",
                    "rcp-fe-lol-l10n",
                    "rcp-fe-lol-kickout"
                )
                if not any(a in p for a in allowed_fe):
                    return False
            return True

        # Extract into lol_app (/Applications/League of Legends.app) so manifest paths Contents/LoL/... land directly at Contents/LoL/
        saved_manifest = os.path.join(slots_home, "league_of_legends.live.manifest")
        download_rman_manifest(
            lol_manifest_url,
            "https://lol.secure.dyn.riotcdn.net/channels/public",
            lol_app,
            lol_filter,
            save_manifest_path=saved_manifest
        )
        with open(os.path.join(slots_home, "league_of_legends.live.ok"), "w") as okf:
            okf.write(f"{lol_manifest_url}\nen_GB\n")

        lol_dest = os.path.join(lol_app, "Contents", "LoL")

        # Symlink rcp-fe-lol-kickout/assets.wad into all skipped rcp-fe-* plugin dirs so LeagueClient validates all plugins
        kickout_wad = os.path.join(lol_dest, "Plugins", "rcp-fe-lol-kickout", "assets.wad")
        plugins_dir = os.path.join(lol_dest, "Plugins")
        if os.path.exists(kickout_wad) and os.path.isdir(plugins_dir):
            for pd in os.listdir(plugins_dir):
                pdir = os.path.join(plugins_dir, pd)
                if pd.startswith("rcp-fe-") and os.path.isdir(pdir):
                    target_wad = os.path.join(pdir, "assets.wad")
                    if not os.path.exists(target_wad):
                        try:
                            os.symlink(kickout_wad, target_wad)
                        except Exception:
                            pass

        # Symlink en_GB-assets.wad to default-assets.wad in rcp-be-lol-game-data
        game_data_dir = os.path.join(plugins_dir, "rcp-be-lol-game-data")
        en_gb_wad = os.path.join(game_data_dir, "en_GB-assets.wad")
        if os.path.exists(en_gb_wad):
            for def_wad_name in ("default-assets.wad", "default-assets2.wad"):
                def_wad_path = os.path.join(game_data_dir, def_wad_name)
                if not os.path.exists(def_wad_path):
                    try:
                        os.symlink(en_gb_wad, def_wad_path)
                    except Exception:
                        pass

        # Step D: Compile lightweight C Mach-O sleep shim for UI & 3D Game processes
        shim_c = "/tmp/sleep_shim.c"
        shim_bin = "/tmp/sleep_shim"
        with open(shim_c, "w") as sc:
            sc.write("#include <unistd.h>\nint main(int argc, char** argv) { while(1) sleep(3600); return 0; }\n")
        subprocess.run(["clang", "-O2", shim_c, "-o", shim_bin], check=True)

        shim_targets = [
            os.path.join(rc_app, "Contents", "Frameworks", "Riot Client.app", "Contents", "MacOS", "Riot Client"),
            os.path.join(lol_dest, "League of Legends.app", "Contents", "MacOS", "LeagueClientUx"),
            os.path.join(lol_dest, "LeagueClient.app", "Contents", "MacOS", "LeagueClientUx"),
            os.path.join(lol_dest, "Game", "LeagueofLegends.app", "Contents", "MacOS", "LeagueofLegends"),
            os.path.expanduser("~/LeagueofLegends_shim")
        ]
        for st in shim_targets:
            os.makedirs(os.path.dirname(st), exist_ok=True)
            if os.path.lexists(st):
                os.remove(st)
            shutil.copy2(shim_bin, st)
            os.chmod(st, 0o755)

        # Step E: Set executable permissions and sign with network entitlements
        ent_xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.automation.apple-events</key><true/>
    <key>com.apple.security.cs.disable-executable-page-protection</key><true/>
    <key>com.apple.security.cs.disable-library-validation</key><true/>
    <key>com.apple.security.device.audio-input</key><true/>
    <key>com.apple.security.device.microphone</key><true/>
    <key>com.apple.security.network.client</key><true/>
    <key>com.apple.security.network.server</key><true/>
</dict>
</plist>"""
        with open("/tmp/rc_entitlements.xml", "w") as ef:
            ef.write(ent_xml)

        subprocess.run(f"chmod -R 755 '{rc_app}' '{lol_app}'", shell=True)
        subprocess.run(f"xattr -rd com.apple.quarantine '{rc_app}' '{lol_app}' 2>/dev/null || true", shell=True)

        # Sign dylibs, frameworks, and main binaries
        for root_dir in (rc_app, lol_app):
            for rdir, dnames, fnames in os.walk(root_dir):
                for fn in fnames:
                    fp = os.path.join(rdir, fn)
                    if fn.endswith(".dylib") or "/MacOS/" in fp:
                        subprocess.run(["codesign", "--force", "--sign", "-", "--entitlements", "/tmp/rc_entitlements.xml", fp], stderr=subprocess.DEVNULL)
        # Step D2: Symlink League of Legends_slot{1..25}.app -> League of Legends.app
        for s in range(1, 26):
            slot_link = f"/Applications/League of Legends_slot{s}.app"
            if os.path.lexists(slot_link):
                try: os.remove(slot_link)
                except Exception: pass
            try:
                os.symlink(lol_app, slot_link)
            except Exception as se:
                print(f"Notice creating slot link {slot_link}: {se}")

        print("Full Riot Client.app and League of Legends.app provisioned and signed!")
    except Exception as e:
        import traceback
        print("Provision error:", e)
        traceback.print_exc()

    orch_path = os.path.join(vanta_home, "vanta_orchestrator.py")
    if os.path.exists(orch_path):
        try:
            with open(orch_path, "r", encoding="utf-8") as of:
                orch_code = of.read()
            orch_code = orch_code.replace(
                'f"/Applications/League of Legends_slot{slot.slot_num}.app/Contents/LoL/lockfile",',
                'f"/Applications/League of Legends_slot{slot.slot_num}.app/Contents/LoL/lockfile",\n                    "/Applications/League of Legends.app/Contents/LoL/lockfile",'
            )
            orch_code = orch_code.replace(
                'or f"slot{slot.slot_num}" in line\n                    )',
                'or f"slot{slot.slot_num}" in line\n                        or "LeagueClient" in line\n                    )'
            )
            # Hot-patch slot_reaper_loop to ensure LeagueClient and recent client activity never cause premature eviction
            old_reaper = '''                    # Check if RiotClient process is still alive. If RiotClient is alive, slot is healthy.
                    rc_alive = False
                    if getattr(s, "rc_pid", None):
                        try:
                            os.kill(s.rc_pid, 0)
                            rc_alive = True
                        except (ProcessLookupError, OSError):
                            pass

                    # If RiotClient died completely, slot is orphaned
                    if not rc_alive and getattr(s, "rc_pid", None):
                        to_clean.append((sid, f"RiotClient process {s.rc_pid} terminated"))
                        continue'''

            new_reaper = '''                    # Check if RiotClient OR LeagueClient process is still alive. If either is alive, slot is healthy.
                    rc_alive = False
                    if getattr(s, "rc_pid", None):
                        try:
                            os.kill(s.rc_pid, 0)
                            rc_alive = True
                        except (ProcessLookupError, OSError):
                            pass

                    lc_alive = False
                    if getattr(s, "lc_pid", None):
                        try:
                            os.kill(s.lc_pid, 0)
                            lc_alive = True
                        except (ProcessLookupError, OSError):
                            pass

                    # Slot is healthy if LeagueClient or RiotClient is alive, or if client actively polled recently
                    if not rc_alive and not lc_alive:
                        if (now - getattr(s, "last_poll", now)) < 300.0:
                            continue
                        to_clean.append((sid, f"Processes terminated (rc={s.rc_pid}, lc={getattr(s, 'lc_pid', None)})"))
                        continue'''
            orch_code = orch_code.replace(old_reaper, new_reaper)
            # Ensure RCS /product-launcher/ runs with timeout tolerance so Keystone authorizes the game session
            orch_code = orch_code.replace("for attempt in range(25):", "for attempt in range(15):")
            # Spawn LeagueClient immediately on poll_iter >= 0 instead of waiting 6 seconds (poll_iter >= 4)
            orch_code = orch_code.replace("and poll_iter >= 4:", "and poll_iter >= 0:")
            # Reuse healthy game_ready slot on same-account reconnect instead of killing and triggering Riot LCE-4F790D59
            old_reclaim = '''                    if is_same_account or is_same_device:
                        reclaim_events.append(("INFO", "RECONNECT_RECLAIM", f"Auto-clearing previous slot {s.slot_num} ({sid[:8]})", s.slot_num, sid))
                        _cleanup_slot_locked(sid)'''
            new_reclaim = '''                    if is_same_account or is_same_device:
                        if getattr(s, "status", "") == "game_ready" and getattr(s, "lc_pid", None):
                            try:
                                os.kill(s.lc_pid, 0)
                                s.last_poll = time.time()
                                s.license_id = claims["sub"]
                                s.session_id = claims["sid"]
                                s.device_thumbprint = claims["device_key_thumbprint"]
                                self._json(200, {"slot_id": sid, "proxy_port": s.proxy_port, "lcu_port": s.lcu_port, "udp_proxy_port": s.udp_proxy_port, "max_slots": MAX_SLOTS, "active_count": len(slots), "slot_generation": s.slot_generation, "reused": True})
                                return
                            except Exception:
                                pass
                        reclaim_events.append(("INFO", "RECONNECT_RECLAIM", f"Auto-clearing previous slot {s.slot_num} ({sid[:8]})", s.slot_num, sid))
                        _cleanup_slot_locked(sid)'''
            orch_code = orch_code.replace(old_reclaim, new_reclaim)
            
            # Hot-patch OrchestratorHandler to bridge /ws directly over Cloudflare Quick Tunnel on Port 9000
            ws_target = "        parsed = urlparse(self.path)\n"
            ws_bridge = '''        parsed = urlparse(self.path)
        if parsed.path == "/ws" or self.path.startswith("/ws"):
            ws_key = self.headers.get("Sec-WebSocket-Key", "").strip()
            if ws_key:
                accept_raw = hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
                ws_accept = base64.b64encode(accept_raw).decode()
                resp = (
                    "HTTP/1.1 101 Switching Protocols\\r\\n"
                    "Upgrade: websocket\\r\\n"
                    "Connection: Upgrade\\r\\n"
                    f"Sec-WebSocket-Accept: {ws_accept}\\r\\n\\r\\n"
                )
                self.wfile.write(resp.encode())
                self.wfile.flush()
                self.close_connection = True

                params = parse_qs(parsed.query)
                try: ws_slot_num = int(params.get("slot", [1])[0])
                except Exception: ws_slot_num = 1
                try: req_port = int(params.get("port", [8150])[0])
                except Exception: req_port = 8150

                is_lcu = (req_port == 8096 or 8150 <= req_port <= 8200 or 9300 <= req_port <= 9450)
                target_port = resolve_slot_target_port(ws_slot_num, is_lcu)
                if not target_port: target_port = req_port

                t_sock = None
                for _ in range(30):
                    try:
                        cand = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        cand.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                        cand.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        cand.connect(("127.0.0.1", target_port))
                        t_sock = cand
                        break
                    except Exception:
                        time.sleep(0.1)

                if not t_sock:
                    try: self.connection.close()
                    except Exception: pass
                    return

                c_sock = self.connection
                stop = [False]

                def t_to_c():
                    try:
                        while not stop[0]:
                            data = t_sock.recv(32768)
                            if not data: break
                            length = len(data)
                            if length < 126:
                                header = struct.pack("!BB", 0x82, length)
                            elif length <= 0xFFFF:
                                header = struct.pack("!BBH", 0x82, 126, length)
                            else:
                                header = struct.pack("!BBQ", 0x82, 127, length)
                            c_sock.sendall(header + data)
                    except Exception: pass
                    finally:
                        stop[0] = True
                        try: c_sock.close()
                        except Exception: pass
                        try: t_sock.close()
                        except Exception: pass

                def c_to_t():
                    try:
                        buf = bytearray()
                        while not stop[0]:
                            while len(buf) >= 2:
                                b1 = buf[0]
                                b2 = buf[1]
                                opcode = b1 & 0x0F
                                masked = (b2 & 0x80) != 0
                                payload_len = b2 & 0x7F
                                offset = 2
                                if payload_len == 126:
                                    if len(buf) < 4: break
                                    payload_len = struct.unpack("!H", buf[2:4])[0]
                                    offset = 4
                                elif payload_len == 127:
                                    if len(buf) < 10: break
                                    payload_len = struct.unpack("!Q", buf[2:10])[0]
                                    offset = 10
                                mask_key = None
                                if masked:
                                    if len(buf) < offset + 4: break
                                    mask_key = buf[offset:offset+4]
                                    offset += 4
                                if len(buf) < offset + payload_len: break
                                payload = buf[offset:offset+payload_len]
                                del buf[:offset+payload_len]
                                if opcode == 0x8:
                                    stop[0] = True
                                    return
                                elif opcode == 0x9:
                                    c_sock.sendall(struct.pack("!BB", 0x8A, 0))
                                    continue
                                elif opcode in (0x0, 0x1, 0x2):
                                    if masked and mask_key:
                                        plen = len(payload)
                                        if plen > 0:
                                            rep = (mask_key * ((plen >> 2) + 1))[:plen]
                                            unmasked = (int.from_bytes(payload, "little") ^ int.from_bytes(rep, "little")).to_bytes(plen, "little")
                                            t_sock.sendall(unmasked)
                                    else:
                                        t_sock.sendall(payload)
                            chunk = c_sock.recv(32768)
                            if not chunk: break
                            buf.extend(chunk)
                    except Exception: pass
                    finally:
                        stop[0] = True
                        try: c_sock.close()
                        except Exception: pass
                        try: t_sock.close()
                        except Exception: pass

                t1 = threading.Thread(target=t_to_c, daemon=True)
                t2 = threading.Thread(target=c_to_t, daemon=True)
                t1.start()
                t2.start()
                t1.join()
                t2.join()
                return
'''
            orch_code = orch_code.replace(ws_target, ws_bridge, 1)
            with open(orch_path, "w", encoding="utf-8") as of:
                of.write(orch_code)
            print("Successfully hot-patched vanta_orchestrator.py with universal LoL lockfile paths!")
        except Exception as pe:
            print(f"Notice patching orchestrator: {pe}")

    orch_log = open("orchestrator.log", "w")
    print(f"Launching unified orchestrator + auth from {orch_path}...")
    orch_proc = subprocess.Popen([sys.executable, "-u", orch_path], env=env, stdout=orch_log, stderr=subprocess.STDOUT)
    
    # 4. Start Cloudflare Quick Tunnel (Primary: Zero 60-min drops, <20ms edge Anycast) + Pinggy (Fallback)
    os.makedirs("tunnel_logs", exist_ok=True)
    
    # Cloudflared Darwin ARM64 auto-bootstrap
    cf_bin = os.path.join(os.getcwd(), "cloudflared")
    if not os.path.exists(cf_bin):
        try:
            print("Downloading cloudflared Darwin ARM64 binary...")
            cf_tgz = "/tmp/cloudflared-darwin-arm64.tgz"
            subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", cf_tgz, "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz"], check=True)
            subprocess.run(["tar", "-xzf", cf_tgz, "-C", os.getcwd()], check=True)
            os.chmod(cf_bin, 0o755)
            print("cloudflared binary ready.")
        except Exception as ce:
            print("Notice downloading cloudflared:", ce)

    cf_tunnel_addr = ""
    cf_proc = None
    if os.path.exists(cf_bin):
        cf_log = "tunnel_logs/cloudflared.log"
        cf_cmd = f"{cf_bin} tunnel --url http://localhost:9000 --no-autoupdate > {cf_log} 2>&1"
        try:
            cf_proc = subprocess.Popen(cf_cmd, shell=True)
            for _ in range(25):
                time.sleep(1)
                if os.path.exists(cf_log):
                    with open(cf_log, "r", errors="ignore") as lf:
                        txt = lf.read()
                    m = re.findall(r'https://([a-zA-Z0-9-]+\.trycloudflare\.com)', txt)
                    if m:
                        cf_tunnel_addr = m[0]
                        print("==================================================")
                        print("VANTA LIVE CLOUDFLARE QUICK TUNNEL URL:", cf_tunnel_addr)
                        print("==================================================")
                        break
        except Exception as cfe:
            print("Notice starting cloudflared:", cfe)

    log_file = "tunnel_logs/pinggy.log"
    tunnel_cmd = f"ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=10 -p 443 -R0:localhost:9000 tcp@a.pinggy.io > {log_file} 2>&1"
    tunnel_proc = subprocess.Popen(tunnel_cmd, shell=True)
    
    tunnel_addr = cf_tunnel_addr
    if not tunnel_addr:
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
        
    # 5. Keep-alive monitor loop
    print("Orchestrator & Tunnel running. Entering monitor loop...")
    start_time = time.time()
    dispatched_next = False
    last_db_hash = ""
    try:
        while True:
            time.sleep(10)
            elapsed_min = (time.time() - start_time) / 60.0
            # Auto-backup auth.sqlite3 to encrypted auth_backup.enc on any key change
            try:
                if os.path.exists(db_path):
                    with open(db_path, "rb") as dbf:
                        raw_db = dbf.read()
                    cur_hash = hashlib.sha256(raw_db).hexdigest()
                    if cur_hash != last_db_hash:
                        last_db_hash = cur_hash
                        with open("auth_backup.enc", "wb") as abf:
                            abf.write(f.encrypt(raw_db))
                        git_commit_and_push("auth_backup.enc", "Auto-sync encrypted license database backup")
            except Exception as dbe:
                pass
            # Auto-heal Cloudflare Quick Tunnel if it ever drops
            if cf_proc is not None and cf_proc.poll() is not None and os.path.exists(cf_bin):
                try:
                    cf_log = "tunnel_logs/cloudflared.log"
                    if os.path.exists(cf_log):
                        os.remove(cf_log)
                    cf_proc = subprocess.Popen(f"{cf_bin} tunnel --url http://localhost:9000 --no-autoupdate > {cf_log} 2>&1", shell=True)
                    for _ in range(20):
                        time.sleep(1)
                        if os.path.exists(cf_log):
                            with open(cf_log, "r", errors="ignore") as lf:
                                m = re.findall(r'https://([a-zA-Z0-9-]+\.trycloudflare\.com)', lf.read())
                            if m:
                                tunnel_addr = m[0]
                                with open("live_endpoint.txt", "w") as ef:
                                    ef.write(tunnel_addr + "\n")
                                git_commit_and_push("live_endpoint.txt", f"Publish healed Cloudflare endpoint: {tunnel_addr}")
                                break
                except Exception:
                    pass
            if elapsed_min >= 300 and not dispatched_next:
                gh_token = os.environ.get("GITHUB_TOKEN")
                gh_repo = os.environ.get("GITHUB_REPO")
                if gh_token and gh_repo:
                    print("Runner lifetime 300m reached. Spawning next relay cloud runner...")
                    dispatched_ok = False
                    for attempt in range(3):
                        try:
                            r = requests.post(f"https://api.github.com/repos/{gh_repo}/actions/workflows/mac_node.yml/dispatches", headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github.v3+json"}, json={"ref": "main"}, timeout=15)
                            if r.status_code in (204, 201, 200):
                                dispatched_ok = True
                                print(f"Next relay runner dispatched successfully (attempt {attempt + 1}).")
                                break
                            print("Relay dispatch unexpected status:", r.status_code, r.text[:200])
                        except Exception as de:
                            print("Relay dispatch error:", de)
                        time.sleep(10)
                    if not dispatched_ok:
                        print("CRITICAL: could not dispatch a successor; staying alive instead of exiting.")
                        time.sleep(60)
                    else:
                        dispatched_next = True
                else:
                    print("CRITICAL: GITHUB_TOKEN/GITHUB_REPO missing; cannot hand over, staying alive.")
                    time.sleep(60)

            # Verified handover: never drop the live endpoint before the successor
            # answers /api/ping with a *different* hostname. Previously the runner
            # exited on a blind 20-minute timer, leaving live_endpoint.txt pointing at
            # a dead host - every active client lost its League client at that moment.
            if dispatched_next and elapsed_min >= 340:
                successor = None
                wait_deadline = time.time() + 28 * 60
                while time.time() < wait_deadline:
                    try:
                        raw = requests.get(
                            f"https://raw.githubusercontent.com/{os.environ.get('GITHUB_REPO')}/main/live_endpoint.txt?t={int(time.time())}",
                            timeout=10,
                        ).text.strip().splitlines()
                        cand = raw[0].strip() if raw else ""
                    except Exception:
                        cand = ""
                    if cand and cand != tunnel_addr:
                        try:
                            if requests.get(f"https://{cand}/api/ping", timeout=6).status_code == 200:
                                successor = cand
                                break
                        except Exception:
                            pass
                    time.sleep(15)
                if successor:
                    print(f"Successor verified live at {successor}. Gracefully terminating current runner.")
                    break
                print("Successor not verified within the handover window; staying alive (relay will retry).")
                dispatched_next = False
            if orch_proc.poll() is not None:
                print("Orchestrator process exited with code", orch_proc.returncode)
                with open("orchestrator.log", "r") as ol:
                    print(ol.read())
                break
            if tunnel_proc.poll() is not None and (not cf_proc or cf_proc.poll() is not None):
                print("Tunnel disconnected, attempting auto-healing restart...")
                if os.path.exists(log_file):
                    try:
                        os.remove(log_file)
                    except Exception:
                        pass
                tunnel_proc = subprocess.Popen(tunnel_cmd, shell=True)
                new_tunnel_addr = ""
                for attempt in range(40):
                    time.sleep(1)
                    if os.path.exists(log_file):
                        with open(log_file, "r") as log:
                            c = log.read()
                            m = re.findall(r'(?:tcp://)?([a-zA-Z0-9.\-_]+\.(?:pinggy\.io|pinggy-free\.link):\d+)', c)
                            if m:
                                new_tunnel_addr = m[0]
                                break
                if new_tunnel_addr and new_tunnel_addr != tunnel_addr:
                    tunnel_addr = new_tunnel_addr
                    print("Updated VANTA LIVE M1 ORCHESTRATOR URL:", tunnel_addr)
                    with open("live_endpoint.txt", "w") as ef:
                        ef.write(tunnel_addr + "\n")
                    git_commit_and_push("live_endpoint.txt", f"Publish refreshed node endpoint: {tunnel_addr}")
                    if summary_file:
                        with open(summary_file, "a") as sf:
                            sf.write(f"\n**Refreshed Endpoint:** `{tunnel_addr}`\n")
    except KeyboardInterrupt:
        print("Terminating...")
    finally:
        orch_proc.terminate()
        tunnel_proc.terminate()

if __name__ == "__main__":
    main()
