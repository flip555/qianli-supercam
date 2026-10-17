"""Outbound notifications: Discord and Slack webhooks, and Telegram bots.

Credentials live in notifications.json (0600) beside the other state. They are never
served back to the page - it is told only whether a channel is configured - and none
of them are ever logged.
"""
import json
import os
import threading
import urllib.parse
import urllib.request

CHANNELS = ("discord", "slack", "telegram")


class Notify:
    def __init__(self, path: str):
        self.path = path
        self.cfg = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.cfg = data if isinstance(data, dict) else {}
        except Exception:
            self.cfg = {}

    def save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.cfg, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def status(self) -> dict:
        out = {}
        for name in CHANNELS:
            cfg = self.cfg.get(name) or {}
            detail = ""
            if name == "telegram":
                if cfg.get("token"):
                    detail = "chat %s" % cfg.get("chat_id", "")
            elif cfg.get("webhook"):
                host = urllib.parse.urlparse(cfg["webhook"]).hostname or ""
                detail = host
            out[name] = {"configured": bool(cfg), "detail": detail}
        return out

    def set(self, channel: str, values: dict) -> bool:
        if channel not in CHANNELS:
            return False
        clean = {k: str(v).strip() for k, v in values.items() if str(v).strip()}
        if not clean:
            self.cfg.pop(channel, None)
        else:
            self.cfg[channel] = clean
        self.save()
        return True

    def send(self, channels, text: str) -> None:
        for name in channels:
            cfg = self.cfg.get(name)
            if cfg:
                threading.Thread(target=self._one, args=(name, cfg, text), daemon=True).start()

    def _one(self, name: str, cfg: dict, text: str) -> None:
        try:
            if name in ("discord", "slack"):
                url = cfg.get("webhook")
                if not url:
                    return
                body = json.dumps({"content": text} if name == "discord" else {"text": text}).encode()
                req = urllib.request.Request(url, data=body,
                                             headers={"Content-Type": "application/json"})
            elif name == "telegram":
                token, chat = cfg.get("token"), cfg.get("chat_id")
                if not token or not chat:
                    return
                data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
                req = urllib.request.Request(
                    "https://api.telegram.org/bot%s/sendMessage" % token, data=data)
            else:
                return
            urllib.request.urlopen(req, timeout=8).read()
        except Exception:
            pass
