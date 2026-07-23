"""Encrypt/decrypt the collected data tree for the PUBLIC deployment repo.

The public twin of this repository must not expose the collected dataset
(snapshots, picks, on-chain history), so CI commits it only as per-file
AES-256-GCM blobs under dataenc/, keyed from the DASHBOARD_PASSWORD secret
(the same password that unlocks the hosted dashboard):

    python3 crypt_data.py pack      # data/...  -> dataenc/...enc
    python3 crypt_data.py unpack    # dataenc/  -> data/...

Encryption is DETERMINISTIC: the key comes from PBKDF2 with a fixed salt and
each file's nonce is derived from its plaintext (SIV-style), so an unchanged
file always produces byte-identical ciphertext. That matters for git — the
frozen historical files (completed .jsonl.gz days) re-encrypt to the exact
same blob every run and add nothing to the repository. The only information
this leaks is WHETHER a file changed, which the commit history shows anyway.

pack also prunes dataenc/ entries whose source file no longer exists (e.g.
a day's .jsonl after it is gzipped), so unpack always recreates exactly the
current data tree. unpack never deletes extra plaintext files. In a private
checkout (no ``.public`` marker), unpack also refuses to overwrite an existing
paper-trade ledger because that manually entered file is authoritative.
"""

import argparse
import hashlib
import hmac
import os
import sys

DATA_DIR = "data"
ENC_DIR = "dataenc"
# Only paths tracked in the private repo; local-only state (scanner.db,
# spike_state.json, ...) stays out of the public tree entirely.  The paper
# ledger is manually authored and private-authoritative: sync-data preserves
# the private copy before unpacking deployment ciphertext.
TRACKED = [
    "snapshots", "picks", "onchain.json", "onchain_history.jsonl",
    "paper_trades.jsonl",
]

MAGIC = b"RHENC1\n"
# fixed salt keeps the derived key — and therefore the ciphertext of
# unchanged files — stable across runs; secrecy rests on the password
SALT = bytes.fromhex("8f3a1c9d4b6e2f70a5d8c3b1e9f04762")
PBKDF2_ITERS = 300_000


def derive_key(password):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), SALT, PBKDF2_ITERS)


def encrypt_bytes(key, plaintext):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    # deterministic nonce from the plaintext: identical content -> identical
    # blob; distinct content -> distinct nonce, so GCM nonce reuse never
    # pairs two different messages
    nonce = hmac.new(key, plaintext, hashlib.sha256).digest()[:12]
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def decrypt_bytes(key, blob):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(MAGIC):
        raise ValueError("not a RHENC1 blob")
    body = blob[len(MAGIC):]
    return AESGCM(key).decrypt(body[:12], body[12:], None)


def iter_files(root):
    if os.path.isfile(root):
        yield root
    elif os.path.isdir(root):
        for dirpath, _, names in os.walk(root):
            for n in sorted(names):
                yield os.path.join(dirpath, n)


def write_atomic(path, blob):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)


def pack(key):
    wanted = set()
    changed = 0
    for entry in TRACKED:
        for src in iter_files(os.path.join(DATA_DIR, entry)):
            rel = os.path.relpath(src, DATA_DIR)
            out = os.path.join(ENC_DIR, rel + ".enc")
            wanted.add(os.path.normpath(out))
            with open(src, "rb") as f:
                blob = encrypt_bytes(key, f.read())
            if os.path.exists(out):
                with open(out, "rb") as f:
                    if f.read() == blob:  # deterministic -> byte-identical
                        continue
            write_atomic(out, blob)
            changed += 1
    pruned = 0
    for enc in list(iter_files(ENC_DIR)):
        if os.path.normpath(enc) not in wanted and enc.endswith(".enc"):
            os.remove(enc)
            pruned += 1
    print(f"pack: {len(wanted)} files ({changed} changed, {pruned} pruned) "
          f"-> {ENC_DIR}/")


def unpack(key):
    count = 0
    preserved = 0
    for enc in iter_files(ENC_DIR):
        if not enc.endswith(".enc"):
            continue
        rel = os.path.relpath(enc, ENC_DIR)[:-len(".enc")]
        out = os.path.join(DATA_DIR, rel)
        if (rel == "paper_trades.jsonl" and os.path.exists(out)
                and not os.path.exists(".public")):
            preserved += 1
            continue
        with open(enc, "rb") as f:
            plaintext = decrypt_bytes(key, f.read())
        write_atomic(out, plaintext)
        count += 1
    suffix = f" ({preserved} private file preserved)" if preserved else ""
    print(f"unpack: {count} files -> {DATA_DIR}/{suffix}")


def main():
    ap = argparse.ArgumentParser(description="Encrypt/decrypt the data tree")
    ap.add_argument("mode", choices=["pack", "unpack"])
    args = ap.parse_args()
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        sys.exit("DASHBOARD_PASSWORD is not set")
    key = derive_key(password)
    if args.mode == "pack":
        pack(key)
    else:
        if not os.path.isdir(ENC_DIR):
            sys.exit(f"{ENC_DIR}/ not found — nothing to unpack")
        unpack(key)


if __name__ == "__main__":
    main()
