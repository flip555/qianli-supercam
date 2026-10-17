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
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

ITERATIONS = 200_000

# What a guest may be granted. Camera, update, account and notifications are never
# grantable: those are the owner's, not the technician's.
GUEST_PERMS = ("measure", "snapshot", "display", "reference", "calibration",
               "alarms", "prerecord")
DEFAULT_GUEST_PERMS = {"measure": True, "snapshot": True, "display": True,
                       "reference": True, "calibration": False,
                       "alarms": False, "prerecord": False}
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
        self.cfg = {"enabled": True, "username": "admin", "password": "",
                    "guest": {"enabled": False, "password": ""},
                    "guest_perms": dict(DEFAULT_GUEST_PERMS)}
        self.load()

    # ---------- persistence ----------
    def load(self) -> None:
        try:
            with open(self.path) as f:
                data = json.load(f)
            if isinstance(data.get("guest"), dict):
                self.cfg["guest"].update(data["guest"])
            if isinstance(data.get("guest_perms"), dict):
                for k, v in data["guest_perms"].items():
                    if k in GUEST_PERMS:
                        self.cfg["guest_perms"][k] = bool(v)
            for key in ("enabled", "username", "password", "secret"):
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

    def perms(self) -> dict:
        out = dict(DEFAULT_GUEST_PERMS)
        for k, v in (self.cfg.get("guest_perms") or {}).items():
            if k in GUEST_PERMS:
                out[k] = bool(v)
        return out

    def set_perms(self, values: dict) -> None:
        out = self.perms()
        for k in GUEST_PERMS:
            if k in values:
                out[k] = bool(values[k])
        self.cfg["guest_perms"] = out
        self.save()

    def can(self, role: str, capability: str) -> bool:
        if role == "admin":
            return True
        if role != "guest" or capability not in GUEST_PERMS:
            return False
        return bool(self.perms().get(capability))

    def set_guest(self, enabled: bool, password: str = None) -> None:
        guest = self.cfg.setdefault("guest", {"enabled": False, "password": ""})
        guest["enabled"] = bool(enabled)
        if password:
            guest["password"] = hash_password(password)
        elif not enabled:
            guest["password"] = ""
        self.save()

    # ---------- sessions ----------
    # Stateless signed cookies: the token carries role/user/expiry and an HMAC over
    # it, so a relay restart no longer signs everybody out (which it did constantly
    # while the stall watchdog was restarting the process).

    def _secret(self) -> bytes:
        secret = self.cfg.get("secret")
        if not secret:
            secret = secrets.token_hex(32)
            self.cfg["secret"] = secret
            self.save()
        return secret.encode()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret(), payload.encode(), hashlib.sha256).hexdigest()
    def login(self, username: str, password: str):
        if not self.enabled():
            return self._new_session("admin", self.cfg.get("username") or "admin")
        username = (username or "").strip()
        if username == self.cfg.get("username") and check_password(password, self.cfg.get("password") or ""):
            return self._new_session("admin", self.cfg.get("username") or username)
        guest = self.cfg.get("guest") or {}
        if (guest.get("enabled") and guest.get("password")
                and (username == "guest" or username == guest.get("name", "guest"))
                and check_password(password, guest["password"])):
            return self._new_session("guest", "guest")
        return None

    def _new_session(self, role: str, user: str) -> str:
        data = {"r": role, "u": user, "e": int(time.time()) + SESSION_TTL}
        payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
        return payload + "." + self._sign(payload)

    def session(self, token: str):
        try:
            payload, sig = (token or "").split(".", 1)
            if not hmac.compare_digest(self._sign(payload), sig):
                return None
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if int(data.get("e", 0)) < time.time():
                return None
            return {"role": data.get("r", "guest"), "user": data.get("u", "guest")}
        except Exception:
            return None

    def logout(self, token: str) -> None:
        """Stateless tokens: the browser drops the cookie, nothing to revoke here."""
        return None

    def reset(self) -> None:
        """Recovery: forget the password and go back to the setup page."""
        self.cfg["password"] = ""
        self.cfg["enabled"] = True
        self.cfg["guest"] = {"enabled": False, "password": ""}
        self.cfg["secret"] = secrets.token_hex(32)   # invalidates every issued cookie
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
