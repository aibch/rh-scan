"""Encrypt report.html into a password-gated static page (docs/index.html).

The output is a self-contained page: an AES-256-GCM payload plus a password
prompt that derives the key in-browser (WebCrypto, PBKDF2-SHA256) and renders
the decrypted dashboard. Safe to host on public GitHub Pages: without the
password the payload is ciphertext.

The public deployment also encrypts its underlying tracked data separately via
``crypt_data.py``. Keep both controls: this file protects the rendered page;
the data pack prevents raw market and paper-trade records from being published.

Usage:
    DASHBOARD_PASSWORD=... python3 encrypt_report.py [--in report.html]
                                                     [--out docs/index.html]

Requires the 'cryptography' package (pip install cryptography).
"""

import argparse
import base64
import json
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

ITERATIONS = 300_000

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Robinhood Chain Scanner</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#0d0d0d; color:#fff;
         font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  .gate {{ background:#1a1a19; border:1px solid rgba(255,255,255,.1);
          border-radius:10px; padding:32px 36px; max-width:340px; }}
  h1 {{ font-size:17px; margin:0 0 4px; }}
  p {{ color:#c3c2b7; font-size:13px; margin:0 0 16px; }}
  input {{ width:100%; box-sizing:border-box; padding:9px 12px; font-size:15px;
          background:#0d0d0d; color:#fff; border:1px solid rgba(255,255,255,.2);
          border-radius:6px; }}
  input:focus {{ outline:2px solid #3987e5; border-color:transparent; }}
  button {{ width:100%; margin-top:10px; padding:9px; font-size:14px;
           font-weight:600; color:#fff; background:#2a78d6; border:0;
           border-radius:6px; cursor:pointer; }}
  button:hover {{ background:#3987e5; }}
  .err {{ color:#e66767; font-size:13px; min-height:18px; margin:8px 0 0; }}
</style></head><body>
<form class="gate" id="gate">
  <h1>Robinhood Chain Scanner</h1>
  <p>This dashboard is password protected.</p>
  <input type="password" id="pw" placeholder="Password" autofocus
         autocomplete="current-password">
  <button type="submit">Unlock</button>
  <p class="err" id="err"></p>
</form>
<script>
const B = {payload};
const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
document.getElementById("gate").addEventListener("submit", async e => {{
  e.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  try {{
    const pw = document.getElementById("pw").value;
    const km = await crypto.subtle.importKey("raw",
      new TextEncoder().encode(pw), "PBKDF2", false, ["deriveKey"]);
    const key = await crypto.subtle.deriveKey(
      {{name:"PBKDF2", salt:b64(B.salt), iterations:B.iter, hash:"SHA-256"}},
      km, {{name:"AES-GCM", length:256}}, false, ["decrypt"]);
    const plain = await crypto.subtle.decrypt(
      {{name:"AES-GCM", iv:b64(B.iv)}}, key, b64(B.ct));
    const html = new TextDecoder().decode(plain);
    document.open(); document.write(html); document.close();
  }} catch (_) {{
    err.textContent = "Wrong password.";
  }}
}});
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default="report.html")
    ap.add_argument("--out", dest="out", default=os.path.join("docs", "index.html"))
    args = ap.parse_args()

    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        raise SystemExit("DASHBOARD_PASSWORD not set")
    with open(args.src, encoding="utf-8") as f:
        plaintext = f.read().encode()

    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=ITERATIONS)
    key = kdf.derive(password.encode())
    ct = AESGCM(key).encrypt(iv, plaintext, None)

    payload = json.dumps({
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "iter": ITERATIONS,
        "ct": base64.b64encode(ct).decode(),
    })
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(PAGE.format(payload=payload))
    os.replace(tmp, args.out)
    print(f"encrypted {args.src} -> {args.out} "
          f"({len(ct)//1024}KB ciphertext, AES-256-GCM, PBKDF2 {ITERATIONS:,})")


if __name__ == "__main__":
    main()
