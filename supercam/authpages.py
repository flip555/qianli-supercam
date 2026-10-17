"""Minimal, self-contained pages for sign-in, first-run setup and account settings.

Kept out of relay.py so the HTTP plumbing there stays readable; they share the
GUI's dark styling and contain no scripts at all.
"""

STYLE = """
 body{background:#0b0d10;color:#e6e8eb;font:15px/1.5 system-ui,sans-serif;margin:0}
 .wrap{min-height:100vh;display:flex;align-items:center;justify-content:center}
 form,section{background:#12171e;border:1px solid #1e242c;border-radius:12px}
 form{width:340px;padding:24px 26px 18px}
 h1{font-size:18px;margin:0 0 6px;font-weight:600}
 p.lead{color:#8b949e;font-size:13px;margin:0 0 18px}
 label{display:block;margin:0 0 12px;color:#8b949e;font-size:13px}
 input{display:block;width:100%;box-sizing:border-box;margin-top:5px;padding:8px 10px;background:#0b0d10;
       border:1px solid #2b3542;border-radius:8px;color:#e6e8eb;font-size:14px}
 button{width:100%;margin-top:6px;padding:9px;background:#1f6feb;border:0;border-radius:8px;color:#fff;
        font-size:14px;font-weight:600;cursor:pointer}
 button.ghost{background:#242c37;color:#c8d1dc}
 .err{color:#ff9d9d;font-size:13px;min-height:18px;margin:10px 0 0}
 .msg{color:#7ee2a8;font-size:13px;min-height:18px;margin:0 0 12px}
 .sub{color:#5c6673;font-size:12px;margin:12px 0 0;line-height:1.45}
 a{color:#8b949e}
 .account{max-width:560px;margin:40px auto;padding:0 18px}
 .account section{padding:16px 18px;margin:0 0 14px}
 .account h2{font-size:14px;margin:0 0 10px;color:#c8d1dc}
 .row{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap}
 .row label{flex:1 1 150px;margin:0}
 .row button{width:auto;flex:0 0 auto;margin:0}
 .who{color:#8b949e;font-size:13px;margin:0 0 20px}
 .permlabel{display:block;margin:0 0 6px;color:#c8d1dc;font-size:13px}
"""


def login(error: str = "") -> str:
    return """<!doctype html><meta charset=utf-8><title>SuperCam — sign in</title>
<style>%s</style><div class=wrap><form method=post action=/login>
<h1>SuperCam — sign in</h1>
<label>Username<input name=username autocomplete=username autofocus required></label>
<label>Password<input type=password name=password autocomplete=current-password required></label>
<button type=submit>Sign in</button>
<p class=err>%s</p>
<p class=sub>A read-only guest account may sign in here too.</p>
</form></div>""" % (STYLE, error)


def setup(error: str = "") -> str:
    return """<!doctype html><meta charset=utf-8><title>SuperCam — set up sign-in</title>
<style>%s</style><div class=wrap><form method=post action=/setup>
<h1>Set up sign-in</h1>
<p class=lead>No password is set, so the camera is not being served yet.</p>
<label>Username<input name=username value=admin autocomplete=username required></label>
<label>Password<input type=password name=password autocomplete=new-password minlength=8 required></label>
<label>Confirm<input type=password name=confirm autocomplete=new-password minlength=8 required></label>
<button type=submit>Save and sign in</button>
<p class=err>%s</p>
<p class=sub>Lost it later? On the host run <b>python -m supercam.auth --reset</b> to reopen this page.</p>
</form></div>""" % (STYLE, error)


PERM_LABELS = (("measure", "Measure — draw points, boxes and polygons"),
               ("snapshot", "Take snapshots"),
               ("display", "Palette, bands and isotherm"),
               ("reference", "Reference frame and ΔT view"),
               ("calibration", "Two-point temperature calibration"),
               ("alarms", "Alarm rules and clips"),
               ("prerecord", "Clip window settings"))


def _perm_rows(perms) -> str:
    perms = perms or {}
    rows = []
    for key, label in PERM_LABELS:
        checked = " checked" if perms.get(key) else ""
        rows.append('<label class=permlabel><input type=checkbox name="p_%s"%s> %s</label>'
                    % (key, checked, label))
    return "".join(rows)


def account(username: str, role: str, enabled: bool, guest_enabled: bool,
            message: str = "", error: bool = False, perms: dict = None) -> str:
    login_state = "Sign-in is <b>required</b>." if enabled else "Sign-in is <b>off</b> — anyone who can reach this relay has full control."
    login_action = "Turn sign-in off" if enabled else "Require sign-in"
    login_next = "0" if enabled else "1"
    guest_state = "A guest can watch, change the view and take snapshots, but cannot change settings or the camera."
    guest_action = "Remove guest" if guest_enabled else "Set guest password"
    guest_next = "0" if guest_enabled else "1"
    return """<!doctype html><meta charset=utf-8><title>SuperCam — account</title>
<style>%s</style><div class=account>
<h1>Account</h1>
<p class=who>Signed in as <b>%s</b> (%s) · <a href="/">camera</a> · <a href="/logout">sign out</a></p>
<p class="%s">%s</p>
<section><h2>Username</h2>
 <form method=post action=/account class=row>
  <input type=hidden name=action value=username>
  <label>New username<input type=text name=username value="%s" required></label>
  <button type=submit>Save</button>
 </form></section>
<section><h2>Password</h2>
 <form method=post action=/account class=row>
  <input type=hidden name=action value=password>
  <label>Current<input type=password name=current required></label>
  <label>New<input type=password name=new autocomplete=new-password minlength=8 required></label>
  <label>Confirm<input type=password name=confirm autocomplete=new-password minlength=8 required></label>
  <button type=submit>Change</button>
 </form></section>
<section><h2>Sign-in requirement</h2>
 <p class=sub style="margin:0 0 10px">%s</p>
 <form method=post action=/account class=row>
  <input type=hidden name=action value=login>
  <input type=hidden name=enabled value=%s>
  <button type=submit class=ghost>%s</button>
 </form></section>
<section><h2>Guest permissions</h2>
 <p class=sub style="margin:0 0 10px">What a guest may do. Camera settings, the update,
 the account and notification credentials always stay with the admin.</p>
 <form method=post action=/account style="display:block;background:none;border:0;padding:0;width:auto">
  <input type=hidden name=action value=perms>
  %s
  <button type=submit style="width:auto;margin-top:6px">Save permissions</button>
 </form></section>
<section><h2>Guest (read only)</h2>
 <p class=sub style="margin:0 0 10px">%s</p>
 <form method=post action=/account class=row>
  <input type=hidden name=action value=guest>
  <input type=hidden name=enabled value=%s>
  <label>%s<input type=password name=password autocomplete=new-password minlength=8 %s></label>
  <button type=submit>%s</button>
 </form></section>
</div>""" % (STYLE, username, role, "err" if error else "msg", message,
             username, login_state, login_next, login_action,
             _perm_rows(perms), guest_state, guest_next,
             "New guest password" if not guest_enabled else "New password (optional)",
             "required" if not guest_enabled else "", guest_action)
