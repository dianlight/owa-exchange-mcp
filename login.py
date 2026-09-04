#!/usr/bin/env python3
"""
Exchange Login Script with Encrypted Credentials
Usage: python3 login.py
Approve login via 2FA mobile app.
"""
import sys
import os
import time
import base64
import getpass
from pathlib import Path
from urllib.parse import urlparse
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

CREDS_FILE = Path(__file__).parent / ".credentials.enc"
SALT_FILE = Path(__file__).parent / ".salt"

def get_key(password: str, salt: bytes) -> bytes:
    """Derive encryption key from password"""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))

def encrypt_credentials(username: str, password: str, master_password: str):
    """Encrypt and save credentials"""
    salt = os.urandom(16)
    key = get_key(master_password, salt)
    f = Fernet(key)

    data = f"{username}:{password}".encode()
    encrypted = f.encrypt(data)

    SALT_FILE.write_bytes(salt)
    CREDS_FILE.write_bytes(encrypted)
    os.chmod(SALT_FILE, 0o600)
    os.chmod(CREDS_FILE, 0o600)
    print("Credentials encrypted and saved.")

def decrypt_credentials(master_password: str) -> tuple:
    """Decrypt and return credentials"""
    if not CREDS_FILE.exists() or not SALT_FILE.exists():
        return None, None

    salt = SALT_FILE.read_bytes()
    encrypted = CREDS_FILE.read_bytes()
    key = get_key(master_password, salt)
    f = Fernet(key)

    try:
        data = f.decrypt(encrypted).decode()
        username, password = data.split(":", 1)
        return username, password
    except:
        print("ERROR: Invalid master password!")
        return None, None

def setup_credentials():
    """Interactive setup of credentials"""
    print("=== Exchange Mail Setup ===")
    username = input("Enter your email: ").strip()
    password = getpass.getpass("Enter your password: ")
    master = getpass.getpass("Create a master password to encrypt credentials: ")
    master2 = getpass.getpass("Confirm master password: ")

    if master != master2:
        print("Passwords don't match!")
        return False

    encrypt_credentials(username, password, master)
    return True

def _default_profile_dir() -> Path:
    return Path(
        os.environ.get("EXCHANGE_BROWSER_PROFILE_DIR")
        or (Path(__file__).parent / ".browser-profile")
    )


def login(username: str, password: str, master_password: str = None):
    """Login to OWA with 2FA (mobile push).

    Runs against the same persistent Chromium profile the MCP server uses
    (EXCHANGE_BROWSER_PROFILE_DIR, or .browser-profile/ by default), so a
    manual `python3 login.py` pre-warms the exact session the server will
    pick up on its next start.
    """
    from playwright.sync_api import sync_playwright

    owa_url = os.environ.get("EXCHANGE_OWA_URL", "")
    if not owa_url:
        print("ERROR: EXCHANGE_OWA_URL environment variable is not set.")
        return False
    owa_host = urlparse(owa_url).netloc

    print(f"Logging in as {username}...", flush=True)

    profile_dir = _default_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile_dir), headless=True)
        pages = context.pages
        page = pages[0] if pages else context.new_page()

        # Step 1: Navigate to OWA (redirects to Microsoft Entra ID sign-in)
        page.goto(f"{owa_url}/owa/", wait_until="networkidle")

        # Step 2a: Email step (loginfmt), then advance to the password step
        page.fill('input[name="loginfmt"]', username)
        try:
            page.click('#idSIButton9', timeout=5000)
        except:
            page.press('input[name="loginfmt"]', 'Enter')
        page.wait_for_load_state("networkidle")

        # Step 2b: Password step (passwd field only becomes fillable here)
        page.wait_for_selector('input[name="passwd"]', state="visible", timeout=15000)
        page.fill('input[name="passwd"]', password)
        try:
            page.click('#idSIButton9', timeout=5000)
        except:
            page.press('input[name="passwd"]', 'Enter')
        page.wait_for_load_state("networkidle")

        print("Credentials submitted, waiting for sign-in to complete...", flush=True)

        # Step 3: Wait for mobile MFA approval if prompted, and for OWA redirect.
        # Along the way, accept the "Stay signed in?" (KMSI) interstitial if it
        # appears - it reuses the same #idSIButton9 id as the previous steps.
        print("Waiting for sign-in to finish... Check your 2FA app if prompted!", flush=True)
        success = False
        kmsi_handled = False
        last_url = ""
        for i in range(90):  # Wait up to 90 seconds
            time.sleep(1)

            try:
                url = page.url

                # Print URL when it changes
                if url != last_url:
                    print(f"  URL changed: {url[:80]}...", flush=True)
                    last_url = url

                # Check if we're at OWA (not at SSO pages). Compare the actual
                # page host, not a substring match on the full URL: SSO login
                # pages often embed the OWA host in a redirect_uri query param,
                # which would falsely match before authentication completes.
                if urlparse(url).netloc == owa_host and "ofam" not in url and "adfs" not in url:
                    print("  OWA detected! Waiting for OWA session to initialize...", flush=True)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    # The document 'load' event only means the SPA shell
                    # rendered - OWA's own session cookies (X-OWA-CANARY
                    # included) are set by background requests fired after
                    # that. Wait for the canary to actually show up, or
                    # every saved session will be SSO-only and every OWA
                    # API call will 401 despite a "successful" login.
                    for _ in range(15):
                        if any(c["name"] == "X-OWA-CANARY" for c in context.cookies()):
                            break
                        time.sleep(1)
                    else:
                        print("  Warning: X-OWA-CANARY cookie not seen after 15s - session may be incomplete.", flush=True)
                        # DIAGNOSTIC: find out how (or whether) this OWA
                        # deployment exposes the canary token, since it's
                        # not showing up as a cookie. Safe to print - the
                        # canary is a CSRF token meant to be client-readable,
                        # not a secret like a session cookie.
                        try:
                            html = page.content()
                            idx = html.lower().find("canary")
                            if idx >= 0:
                                snippet = html[max(0, idx - 80):idx + 150]
                                print(f"  [diag] found 'canary' in page HTML near: ...{snippet}...", flush=True)
                            else:
                                print("  [diag] 'canary' not found anywhere in page HTML.", flush=True)
                        except Exception as diag_e:
                            print(f"  [diag] HTML scan failed: {diag_e}", flush=True)
                    success = True
                    break

                # Also check if page has OWA elements
                try:
                    if page.locator('[aria-label*="Outlook"], [aria-label*="Почта"]').count() > 0:
                        print("  OWA elements detected!", flush=True)
                        success = True
                        break
                except:
                    pass

                # Accept "Stay signed in?" once, if it shows up
                if not kmsi_handled:
                    try:
                        if page.locator('input[name="passwd"]').count() == 0 and page.locator('#idSIButton9').is_visible(timeout=1000):
                            print("  Accepting 'Stay signed in?' prompt...", flush=True)
                            page.click('#idSIButton9', timeout=5000)
                            kmsi_handled = True
                            page.wait_for_load_state("networkidle", timeout=15000)
                    except:
                        pass

                if i > 0 and i % 15 == 0:
                    print(f"  Still waiting... ({i}s)", flush=True)

            except Exception as e:
                err_str = str(e).lower()
                if "navigation" in err_str or "destroyed" in err_str or "target closed" in err_str:
                    print(f"  Navigation in progress...", flush=True)
                    try:
                        page.wait_for_load_state("load", timeout=15000)
                        url = page.url
                        if urlparse(url).netloc == owa_host and "ofam" not in url:
                            success = True
                            break
                    except:
                        pass
                else:
                    print(f"  Error: {e}", flush=True)

        if success:
            print("\n*** SUCCESS! Logged into OWA! ***", flush=True)
            print(f"Session saved in persistent browser profile: {profile_dir}", flush=True)
        else:
            print("Login failed - no approval received within 60 seconds", flush=True)

        context.close()
        return success

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--setup":
        setup_credentials()
        return

    if not CREDS_FILE.exists():
        print("No credentials found. Run with --setup first.")
        sys.exit(1)

    master = os.environ.get("EXCHANGE_MASTER_PASSWORD") or getpass.getpass("Master password: ")
    username, password = decrypt_credentials(master)
    if not username:
        sys.exit(1)

    login(username, password, master_password=master)

if __name__ == "__main__":
    main()
