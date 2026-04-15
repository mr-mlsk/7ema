"""
auth_token.py — Fyers API v3 Automated Token Generator
========================================================
Generates a fresh access token every morning with NO manual steps.
No browser. No copy-paste. Runs silently on AWS EC2 via cron.

HOW FYERS v3 AUTH WORKS (3 steps):
  1. Send login OTP request  →  Fyers sends OTP to your mobile
  2. Verify TOTP code        →  generated from your TOTP secret (pyotp)
  3. Verify PIN              →  your Fyers account PIN (SHA-256 hashed)
  Then exchange auth_code for access_token via SessionModel.

CREDENTIALS NEEDED (stored as env vars — never in code):
  FYERS_USER_ID      Your Fyers client code     e.g. XY12345
  FYERS_PIN          Your Fyers login PIN        e.g. 1234
  FYERS_TOTP_SECRET  Base32 TOTP secret string   e.g. JBSWY3DPEHPK3PXP

  client_id and secret_key stay in config.py (they are app credentials,
  not personal account credentials — safe to store in files).

ONE-TIME SETUP ON AWS EC2:
  pip install pyotp requests
  echo 'export FYERS_USER_ID="XY12345"'           >> ~/.bashrc
  echo 'export FYERS_PIN="1234"'                   >> ~/.bashrc
  echo 'export FYERS_TOTP_SECRET="YOURBASE32KEY"'  >> ~/.bashrc
  source ~/.bashrc

HOW TO GET YOUR TOTP SECRET:
  Fyers App → Profile → My Profile → Security Settings → 2FA
  Click "Can't scan QR code?" → copies text key like JBSWY3DPEHPK3PXP
  That is your TOTP secret.

RUN MANUALLY:
  python auth_token.py

CRON (runs at 8:50 AM IST = 3:20 AM UTC, Mon–Fri):
  20 3 * * 1-5 cd ~/ema7bot && source ~/.bashrc && \
    /home/ubuntu/ema7bot/venv/bin/python auth_token.py \
    >> ~/ema7bot/auth_cron.log 2>&1

FALLBACK:
  If env vars are not set, falls back to manual browser login.
"""

import os
import re
import sys
import time
import hashlib

import requests

sys.path.insert(0, ".")
from config import FYERS_CONFIG
from fyers_apiv3.fyersModel import SessionModel

# ── Fyers v3 login API endpoints ───────────────────────────────────────────────
_BASE = "https://api-t1.fyers.in/api/v3"


# ── Token writing ──────────────────────────────────────────────────────────────

def _write_token(access_token: str):
    """Write access_token into config.py (replaces existing value)."""
    with open("config.py", "r", encoding="utf-8") as f:
        content = f.read()
    updated = re.sub(
        r'("access_token"\s*:\s*")[^"]*(")',
        rf'\g<1>{access_token}\g<2>',
        content,
    )
    with open("config.py", "w", encoding="utf-8") as f:
        f.write(updated)
    print(f"[OK] Token written to config.py  ({access_token[:20]}...)")


# ── Step helpers ───────────────────────────────────────────────────────────────

def _app_id_digits(client_id: str) -> str:
    """
    Extract the numeric app suffix from client_id.
    "XY12345-100" → "100"
    """
    parts = client_id.strip().split("-")
    return parts[-1] if len(parts) > 1 else client_id


def _post(endpoint: str, payload: dict) -> dict:
    """POST to Fyers login API. Raises on HTTP error."""
    url  = f"{_BASE}/{endpoint}"
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("s") not in ("ok", "OK", True, "true"):
        raise RuntimeError(
            f"Fyers API error at /{endpoint}: "
            f"{data.get('message', data)}")
    return data


# ── Automated headless login ───────────────────────────────────────────────────

def auto_login(user_id: str, pin: str, totp_secret: str) -> str:
    """
    Perform headless Fyers v3 login using direct REST API.
    Returns the auth_code to exchange for an access token.

    Step 1: send-login-otp   → triggers OTP to mobile (needed as handshake)
    Step 2: verify-otp       → verify TOTP code from authenticator app
    Step 3: verify-pin       → verify account PIN (SHA-256 hashed)
    Returns auth_code for final token exchange.
    """
    import pyotp

    app_id = _app_id_digits(FYERS_CONFIG["client_id"])
    print(f"[Auth] Starting headless login for user_id={user_id} "
          f"app_id={app_id}")

    # ── Step 1: Send login OTP ─────────────────────────────────────────────────
    print("[Auth] Step 1/3: Sending login OTP...")
    r1 = _post("send-login-otp", {
        "fy_id":  user_id,
        "app_id": app_id,
    })
    request_key = r1.get("request_key", "")
    print(f"[Auth] Step 1 OK — request_key obtained")
    time.sleep(1)   # brief pause before next call

    # ── Step 2: Verify TOTP ────────────────────────────────────────────────────
    totp_code = pyotp.TOTP(totp_secret).now()
    print(f"[Auth] Step 2/3: Verifying TOTP ({totp_code})...")
    r2 = _post("verify-otp", {
        "fyers_id":    user_id,
        "app_id":      app_id,
        "request_key": request_key,
        "otp":         totp_code,
        "source":      "API",
        "send_email":  0,
    })
    request_key2 = r2.get("request_key", request_key)
    print("[Auth] Step 2 OK — TOTP verified")
    time.sleep(1)

    # ── Step 3: Verify PIN ─────────────────────────────────────────────────────
    pin_hash = hashlib.sha256(pin.encode()).hexdigest()
    print("[Auth] Step 3/3: Verifying PIN...")
    r3 = _post("verify-pin", {
        "fyers_id":    user_id,
        "app_id":      app_id,
        "request_key": request_key2,
        "pin":         pin_hash,
        "source":      "API",
    })
    auth_code = r3.get("data", {}).get("authorization_code", "")
    if not auth_code:
        # Some Fyers versions return it at top level
        auth_code = r3.get("auth_code", "")
    if not auth_code:
        raise RuntimeError(
            f"No auth_code in verify-pin response: {r3}")
    print(f"[Auth] Step 3 OK — auth_code obtained")
    return auth_code


# ── Manual browser login fallback ──────────────────────────────────────────────

def manual_login() -> str:
    """Interactive browser-based login. Used when env vars are not set."""
    session = SessionModel(
        client_id    = FYERS_CONFIG["client_id"],
        secret_key   = FYERS_CONFIG["secret_key"],
        redirect_uri = FYERS_CONFIG["redirect_uri"],
        response_type= "code",
        grant_type   = "authorization_code",
    )
    login_url = session.generate_authcode()
    print("\n" + "="*60)
    print("  MANUAL LOGIN — open this URL in your browser:")
    print("="*60)
    print(login_url)
    print("="*60)
    print("\nAfter login, copy the auth_code from the redirect URL:")
    print("  https://127.0.0.1:8080/?auth_code=XXXX&state=None")
    print()
    auth_code = input("  Paste auth_code here: ").strip()
    return auth_code


# ── Exchange auth_code for access_token ────────────────────────────────────────

def exchange_token(auth_code: str) -> str:
    """Exchange auth_code for access_token via Fyers SessionModel."""
    session = SessionModel(
        client_id    = FYERS_CONFIG["client_id"],
        secret_key   = FYERS_CONFIG["secret_key"],
        redirect_uri = FYERS_CONFIG["redirect_uri"],
        response_type= "code",
        grant_type   = "authorization_code",
    )
    session.set_token(auth_code)
    resp = session.generate_token()
    if resp.get("s") not in ("ok", "OK"):
        raise RuntimeError(
            f"Token exchange failed: {resp.get('message', resp)}")
    return resp["access_token"]


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    user_id     = os.environ.get("FYERS_USER_ID", "")
    pin         = os.environ.get("FYERS_PIN", "")
    totp_secret = os.environ.get("FYERS_TOTP_SECRET", "")

    all_set = bool(user_id and pin and totp_secret)

    if all_set:
        print("[Auth] Environment variables found — running headless auto-login")
        try:
            auth_code = auto_login(user_id, pin, totp_secret)
        except Exception as e:
            print(f"\n[ERROR] Auto-login failed: {e}")
            print("        Check FYERS_USER_ID, FYERS_PIN, FYERS_TOTP_SECRET")
            print("        Falling back to manual login...")
            auth_code = manual_login()
    else:
        missing = []
        if not user_id:     missing.append("FYERS_USER_ID")
        if not pin:         missing.append("FYERS_PIN")
        if not totp_secret: missing.append("FYERS_TOTP_SECRET")
        print(f"[Auth] Env vars not set: {', '.join(missing)}")
        print("[Auth] Running manual browser login...")
        auth_code = manual_login()

    print("[Auth] Exchanging auth_code for access_token...")
    access_token = exchange_token(auth_code)
    _write_token(access_token)
    print("\n[Done] Token ready. You can now run:")
    print("       python backtest.py --days 180")
    print("       python forward_test.py")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception as e:
        print(f"\n[FATAL] {e}")
        sys.exit(1)
