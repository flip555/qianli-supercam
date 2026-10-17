"""Authentication for the relay: an admin account and an optional read-only guest.

Deliberately small but real:

* passwords are PBKDF2-SHA256 (per-password salt, 200k iterations), never stored
  or logged in clear;
* sessions are random tokens held in memory, so a restart simply asks again;
* the default is "login enabled, no password set", which serves a one-page setup
  form and nothing else - so a fresh install is never silently open to the network;
* recovery if the password is lost: ``python -m supercam.auth --reset`` on the host
  puts it back into that setup state (login stays enabled).

State lives beside the relay's other files (auth.json, 0600).
"""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import time

ITERATIONS = 200_000
SESSION_TTL = 30 * 24 * 3600


def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2$%d$%s$%s" % (ITERATIONS, salt.hex(), digest.hex())


def check_password(password: str, stored: str) -> bool:
    try:
        alg, iterations, salt_hex, digest_hex = (stored or "").split("$")
        if alg != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


class Auth:
    def __init__(self, path: str):
        self.path = path
        self.sessions = {}
        self.cfg = {"enabled": True, "username": "admin", "password": "",
                    "guest": {"enabled": False, "password": ""}}
        self.load()

    # ---------- persistence ----------
    def load(self) -> None:
        try:
            with open(self.path) as f:
                data = json.load(f)
            if isinstance(data.get("guest"), dict):
                self.cfg["guest"].update(data["guest"])
            for key in ("enabled", "username", "password"):
                if key in data:
                    self.cfg[key] = data[key]
        except Exception:
            pass

    def save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.cfg, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---------- state ----------
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def needs_setup(self) -> bool:
        return self.enabled() and not self.cfg.get("password")

    def set_enabled(self, on: bool) -> None:
        self.cfg["enabled"] = bool(on)
        self.save()

    def set_credentials(self, username: str, password: str) -> None:
        username = (username or "").strip()[:40] or "admin"
        self.cfg["username"] = username
        self.cfg["password"] = hash_password(password)
        self.save()

    def set_guest(self, enabled: bool, password: str = None) -> None:
        guest = self.cfg.setdefault("guest", {"enabled": False, "password": ""})
        guest["enabled"] = bool(enabled)
        if password:
            guest["password"] = hash_password(password)
        elif not enabled:
            guest["password"] = ""
        self.save()

    # ---------- sessions ----------
    def login(self, username: str, password: str):
        if not self.enabled():
            return self._open_session("admin")
        username = (username or "").strip()
        if username == self.cfg.get("username") and check_password(password, self.cfg.get("password") or ""):
            return self._new_session("admin")
        guest = self.cfg.get("guest") or {}
        if (guest.get("enabled") and guest.get("password")
                and (username == "guest" or username == guest.get("name", "guest"))
                and check_password(password, guest["password"])):
            return self._new_session("guest")
        return None

    def _new_session(self, role: str) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {"role": role, "exp": time.time() + SESSION_TTL}
        return token

    def _open_session(self, role: str) -> str:
        return self._new_session(role)

    def session(self, token: str):
        if not token:
            return None
        entry = self.sessions.get(token)
        if not entry:
            return None
        if entry["exp"] < time.time():
            self.sessions.pop(token, None)
            return None
        return entry

    def logout(self, token: str) -> None:
        self.sessions.pop(token or "", None)

    def reset(self) -> None:
        """Recovery: forget the password and go back to the setup page."""
        self.cfg["password"] = ""
        self.cfg["enabled"] = True
        self.cfg["guest"] = {"enabled": False, "password": ""}
        self.sessions.clear()
        self.save()


def _default_path() -> str:
    return os.environ.get("SUPERCAM_AUTH_PATH", "auth.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SuperCam relay account management")
    ap.add_argument("--path", default=_default_path())
    ap.add_argument("--reset", action="store_true",
                    help="forget the password and go back to setup (keeps login enabled)")
    ap.add_argument("--disable", action="store_true", help="turn login off entirely")
    ap.add_argument("--enable", action="store_true", help="turn login on")
    ap.add_argument("--status", action="store_true", help="report the current account state")
    args = ap.parse_args()
    auth = Auth(args.path)
    if args.reset:
        auth.reset()
        print("reset: open %s/ and set a username and password" % "the relay")
    if args.disable:
        auth.set_enabled(False)
        print("login disabled - the relay is open to anyone who can reach it")
    if args.enable:
        auth.set_enabled(True)
        print("login enabled")
    if args.status or not (args.reset or args.disable or args.enable):
        print("path:      %s" % args.path)
        print("enabled:   %s" % auth.enabled())
        print("username:  %s" % auth.cfg.get("username"))
        print("password:  %s" % ("set" if auth.cfg.get("password") else "NOT SET (setup page)"))
        print("guest:     %s" % ("enabled" if (auth.cfg.get("guest") or {}).get("enabled") else "disabled"))
