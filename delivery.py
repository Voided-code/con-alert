"""Sending alerts: Messages (iMessage/SMS), email and WhatsApp, the recipient lists and the alert record."""

import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path


from common import (ALERTS, COUNTRY_CODE, DRY_RUN, PHONE, RECORD, RECORD_COLUMNS, ROOT, STATE, _read_json,
                    log)
# The file reference has to be made OUTSIDE the "tell Messages" block: made inside it, Messages
# sends an empty bubble instead of the picture.
SEND = """
on run argv
  -- number, text, "iMessage" or "SMS", and optionally a picture
  set pic to missing value
  if (count of argv) > 3 then set pic to POSIX file (item 4 of argv)
  tell application "Messages"
    if (item 3 of argv) is "SMS" then
      set svc to 1st account whose service type = SMS
    else
      set svc to 1st account whose service type = iMessage
    end if
    set p to participant (item 1 of argv) of svc
    if (item 2 of argv) is not "" then send (item 2 of argv) to p
    if pic is not missing value then send pic to p
  end tell
end run
"""


# ------------------------------------------------------------- what is it


# -------------------------------------------------------------------- record
def record(spot_name, rows, kind, sent, picture):
    """Append what an alert was about to alerts.csv.
    rows = [(country, relation, units, where, x, y, heading_for, arrives_in_min, sight), ...]"""
    try:
        new_file = not RECORD.exists()
        with RECORD.open("a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(RECORD_COLUMNS)
            stamp = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}"
            for row in rows:
                w.writerow([stamp, spot_name, *row, kind,
                            "dry run" if DRY_RUN else "yes" if sent else "FAILED", str(picture or "")])
    except Exception as ex:
        log(f"could not write {RECORD.name}: {type(ex).__name__}: {ex}")


def record_tail(n=10):
    """The last n rows of alerts.csv as dicts, newest first."""
    try:
        with RECORD.open(newline="") as f:
            return list(csv.DictReader(f))[-n:][::-1]
    except Exception:
        return []


# ------------------------------------------------------------------- message
# ------------------------------------------------------------------- email
# Alerts can go by email instead of iMessage, sent through your own mail account's outgoing server. email.json (readable only by you)
# holds {"from", "password", "to", "host", "port", "on"}; the password never goes in the log or to the page.
EMAIL_FILE = ROOT / "email.json"
MAIL_SERVERS = {"gmail.com": ("smtp.gmail.com", 465), "googlemail.com": ("smtp.gmail.com", 465),
                "icloud.com": ("smtp.mail.me.com", 587), "me.com": ("smtp.mail.me.com", 587), "mac.com": ("smtp.mail.me.com", 587),
                "outlook.com": ("smtp-mail.outlook.com", 587), "hotmail.com": ("smtp-mail.outlook.com", 587),
                "live.com": ("smtp-mail.outlook.com", 587), "yahoo.com": ("smtp.mail.yahoo.com", 465),
                "yahoo.com.au": ("smtp.mail.yahoo.com", 465), "fastmail.com": ("smtp.fastmail.com", 465)}


def email_load():
    try:
        return json.loads(EMAIL_FILE.read_text())
    except Exception:
        return {}


def email_save(d):
    tmp = EMAIL_FILE.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.write_text(json.dumps(d))
    os.chmod(tmp, 0o600)
    tmp.replace(EMAIL_FILE)


def email_state():
    d = email_load()
    return {"on": bool(d.get("on")), "pending": bool(d.get("from") and not d.get("on")),
            "from": d.get("from", ""), "to": d.get("to", ""), "host": d.get("host", "")}


def email_send(d, text, image=None, to=None):
    """Send one email to the list `to` (with the picture attached if given). Raises RuntimeError with the reason."""
    import smtplib
    import ssl
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = f"CON alert <{d['from']}>"
    msg["To"] = ", ".join(to or [d["to"]])
    msg["Subject"] = (text.split(": ", 2)[0] + ": " + text.split(": ", 2)[1] if text.startswith("CON ALERT: ") and text.count(": ") >= 2
                      else text)[:110]
    msg.set_content(text)
    if image and Path(image).exists():
        msg.add_attachment(Path(image).read_bytes(), maintype="image", subtype="jpeg", filename=Path(image).name)
    host, port, local = d["host"], int(d["port"]), d["host"] in ("127.0.0.1", "localhost")
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            elif not local:
                s.close()
                raise RuntimeError("That mail server does not offer an encrypted connection, so the password was not sent")
        with s:
            try:
                # one login attempt only: after a second refused attempt Gmail just hangs up, which hides the reason
                s.ehlo_or_helo_if_needed()                # the server's list of login methods comes with its greeting
                if "PLAIN" in s.esmtp_features.get("auth", "").upper().split():
                    s.user, s.password = d["from"], d["password"]
                    s.auth("PLAIN", s.auth_plain)
                else:
                    s.login(d["from"], d["password"])
            except smtplib.SMTPAuthenticationError as ex:
                said = " ".join(re.sub(r"https?://\S+|\b\d\.\d\.\d+\b|\S+ - gsmtp", "", ex.smtp_error.decode("utf-8", "replace")).split())
                said = said.replace("For more information, go to", "").strip(" .")
                app = "application-specific password" in said.lower() or "app password" in said.lower()
                raise RuntimeError(("The mail server wants an app password, not your normal password. " if app else
                                    "The mail server did not accept that address and password. Gmail, iCloud and Yahoo need an "
                                    "app password made for this (16 letters), not your normal password. ")
                                   + (f'It said: "{said[:140]}".' if said else "")) from None
            except smtplib.SMTPServerDisconnected:
                raise RuntimeError("The mail server hung up during the login, which usually means it did not accept the "
                                   "address and password. Gmail, iCloud and Yahoo need an app password, not your normal one.") from None
            s.send_message(msg)
    except RuntimeError:
        raise
    except smtplib.SMTPRecipientsRefused:
        raise RuntimeError("The mail server refused the address the email was going to. Check it for typing mistakes.") from None
    except smtplib.SMTPResponseException as ex:
        said = " ".join(str(ex.smtp_error.decode("utf-8", "replace") if isinstance(ex.smtp_error, bytes) else ex.smtp_error).split())
        raise RuntimeError(f'The mail server refused the message. It said: "{said[:160]}".') from None
    except smtplib.SMTPException as ex:
        raise RuntimeError(f"The mail server {host} ended the conversation unexpectedly ({type(ex).__name__}). "
                           "If this is not Gmail, iCloud or Yahoo, check the server and port.") from None
    except (OSError, ssl.SSLError) as ex:
        raise RuntimeError(f"The mail server {host} could not be reached ({type(ex).__name__})") from None


def email_setup(sender, password, to="", host="", port=""):
    """Step 1: remember the account and send a test email. Alerts only switch to email after email_confirm()."""
    ok = r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+"
    sender, to, password = str(sender or "").strip(), str(to or "").strip() or str(sender or "").strip(), str(password or "")
    if not re.fullmatch(ok, sender) or not re.fullmatch(ok, to):
        raise RuntimeError("That does not look like an email address.")
    if not password.strip():
        raise RuntimeError("The password is missing.")
    if re.fullmatch(r"\s*(\w{4}\s+){3}\w{4}\s*", password):       # app passwords are shown in groups of four
        password = "".join(password.split())
    host = str(host or "").strip() or MAIL_SERVERS.get(sender.rsplit("@", 1)[1].lower(), ("", 0))[0]
    if not host:
        raise RuntimeError("I do not know the outgoing mail server for that address. Fill in the server (and port) "
                           "from your mail provider's help page, for example smtp.example.com and 587.")
    try:
        port = int(port or MAIL_SERVERS.get(sender.rsplit("@", 1)[1].lower(), ("", 587))[1])
    except ValueError:
        raise RuntimeError("The port must be a number, usually 465 or 587.") from None
    d = {"from": sender, "password": password, "to": to, "host": host, "port": port, "on": False}
    try:
        email_send(d, "CON alert test. If you can read this email, alerts can reach you here.")
    except RuntimeError as ex:
        log(f"email setup failed at {host}:{port}: {ex}")
        raise
    email_save(d)
    return email_state()


def email_confirm():
    d = email_load()
    if not d.get("from"):
        raise RuntimeError("Email is not set up yet.")
    d["on"] = True
    email_save(d)
    r = recipients()
    if d["to"].lower() not in [e.lower() for e in r["emails"]]:      # the tested address starts the list
        recipients_save(r["phones"], r["emails"] + [d["to"]], r["sms"], r["whatsapp"], r["paused"])
    log("email: switched on")
    return email_state()


def email_off():
    was = email_load().get("on")
    EMAIL_FILE.unlink(missing_ok=True)
    r, a = recipients(), recipients(active=True)
    if not a["phones"] and not (wa_load().get("token") and a["whatsapp"]):       # never leave alerts with nowhere to go
        if PHONE:
            recipients_save([PHONE], r["emails"], r["sms"], r["whatsapp"], r["paused"])
    if was:
        log("email: switched off")
    return email_state()


# ---------------------------------------------------------------- WhatsApp
# Alerts can also go out as WhatsApp messages through Meta's official service (the "Cloud API"), from the
# business number of your Meta app. whatsapp.json (readable only by you) holds {"token", "phone_id",
# "from", "waba": business account id, "template", "lang", "tpl_status"}. The token never goes in the log.
#
# WhatsApp's rule: a business number may only write freely to someone who wrote to it in the last 24
# hours. At any other time only a pre-approved "template" message is allowed. So alerts are sent as the
# template below (made automatically, approved by Meta), and the picture is sent after it as a normal
# message, which arrives only when that 24-hour window is open.
WA_FILE = ROOT / "whatsapp.json"
WA_API = "https://graph.facebook.com"
WA_TEMPLATE = "con_alert"
WA_TEMPLATE_TEXT = "CON alert: {{1}}. Open the CON alert map for details."


def wa_number(p):
    """WhatsApp needs the full international number: 0432... (typed the local way) becomes +61432..."""
    digits = re.sub(r"\D", "", str(p))
    if str(p).strip().startswith("+0") or (digits.startswith("0") and not str(p).strip().startswith("+")):
        digits = COUNTRY_CODE.lstrip("+") + digits.lstrip("0")
    return "+" + digits


def wa_load():
    return _read_json(WA_FILE, {})


def wa_save(d):
    tmp = WA_FILE.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.write_text(json.dumps(d))
    os.chmod(tmp, 0o600)
    tmp.replace(WA_FILE)


def wa_call(token, path, fields=None, method="GET", upload=None, timeout=30):
    """One call to Meta's service. Returns its answer; raises RuntimeError with Meta's own reason."""
    import urllib.error
    import urllib.parse
    import urllib.request
    url, headers, data = f"{WA_API}/{path}", {"Authorization": f"Bearer {token}"}, None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(fields or {})
    elif upload:
        b = uuid.uuid4().hex
        parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in (fields or {}).items()]
        p = Path(upload)
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{p.name}"\r\n'
                     f'Content-Type: image/jpeg\r\n\r\n'.encode() + p.read_bytes() + b"\r\n")
        parts.append(f"--{b}--\r\n".encode())
        data, headers["Content-Type"] = b"".join(parts), f"multipart/form-data; boundary={b}"
    else:
        data, headers["Content-Type"] = json.dumps(fields or {}).encode(), "application/json"
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=timeout).read())
    except urllib.error.HTTPError as ex:
        try:
            err = json.loads(ex.read()).get("error", {})
        except Exception:
            err = {}
        if err.get("code") == 190:
            raise RuntimeError("the WhatsApp access token is wrong or has expired. Generate a new one on Meta's page and connect again") from None
        if err.get("code") == 131030:
            raise RuntimeError("that number is not on your Meta app's list of allowed recipients. Add it under Manage recipients on Meta's page") from None
        said = err.get("error_user_msg") or (err.get("error_data") or {}).get("details") or err.get("message") or f"error {ex.code}"
        raise RuntimeError(f'Meta said: "{" ".join(str(said).split())[:220]}"') from None
    except Exception as ex:
        raise RuntimeError(f"Meta's WhatsApp service could not be reached ({type(ex).__name__})") from None


def wa_state():
    d = wa_load()
    st = d.get("tpl_status", "")
    exp = int(d.get("expires") or 0)
    return {"on": bool(d.get("token")), "from": d.get("from", ""), "template": st,
            "ready": st == "APPROVED", "note": d.get("tpl_error", "") if d.get("waba") else "",
            "need_account": bool(d.get("token")) and not d.get("waba"),
            "expires": f"{dt.datetime.fromtimestamp(exp):%-d %b %Y}" if exp else "",
            "expires_soon": bool(exp) and exp - time.time() < 7 * 86400}


def wa_template(d):
    """Make sure the alert template exists in the business account, and note whether Meta has approved it."""
    d.pop("tpl_error", None)
    try:
        if not d.get("waba"):
            raise RuntimeError("the business account behind that number could not be found, so the alert template could not be made")
        got = wa_call(d["token"], f"{d['waba']}/message_templates", {"name": WA_TEMPLATE, "fields": "name,status,language"})
        rows = [t for t in got.get("data", []) if t.get("name") == WA_TEMPLATE]
        if rows:
            d["tpl_status"], d["lang"] = str(rows[0].get("status", "")).upper(), rows[0].get("language", "en_US")
        else:
            wa_call(d["token"], f"{d['waba']}/message_templates", method="POST", fields={
                "name": WA_TEMPLATE, "category": "UTILITY", "language": "en_US",
                "components": [{"type": "BODY", "text": WA_TEMPLATE_TEXT,
                                "example": {"body_text": [["Near 2nd UAV Squadron: China (at war): 1 Corvette at sea"]]}}]})
            d["tpl_status"], d["lang"] = "PENDING", "en_US"
            log("WhatsApp: asked Meta to approve the alert template")
        d["template"] = WA_TEMPLATE
    except RuntimeError as ex:
        d["tpl_error"] = str(ex)
    d["tpl_checked"] = time.time()
    wa_save(d)


def wa_setup(token, phone_id):
    token, phone_id = "".join(str(token or "").split()), re.sub(r"\D", "", str(phone_id or ""))
    if len(token) < 50:
        raise RuntimeError("That does not look like an access token. Press Generate API token on Meta's page and copy the whole long line.")
    if not phone_id:
        raise RuntimeError('The phone number ID is missing. It is the long number shown as "Phone number ID" on Meta\'s page.')
    try:
        info = wa_call(token, phone_id, {"fields": "display_phone_number,verified_name"})
    except RuntimeError as ex:
        log(f"WhatsApp setup failed: {ex}")
        raise RuntimeError("Could not connect: " + str(ex) + ".") from None
    d = {"token": token, "phone_id": phone_id, "from": info.get("display_phone_number", ""), "waba": "",
         "template": "", "lang": "en_US", "tpl_status": ""}
    try:                                                  # when the token runs out, and which business account it can manage
        about = wa_call(token, "debug_token", {"input_token": token}).get("data", {})
        d["expires"] = int(about.get("expires_at") or 0)
        for want in ("whatsapp_business_management", "whatsapp_business_messaging"):
            ids = [i for g in about.get("granular_scopes", []) if g.get("scope") == want for i in g.get("target_ids") or []]
            if ids:
                d["waba"] = str(ids[0])
                break
        if not d["waba"]:                                 # a system user with the account assigned to it
            got = wa_call(token, "me/assigned_whatsapp_business_accounts", {"fields": "id"}).get("data", [])
            if got:
                d["waba"] = str(got[0]["id"])
    except RuntimeError:
        pass
    if not d["waba"]:                                     # the sending number's health report names its account
        try:
            parts = wa_call(token, phone_id, {"fields": "health_status"}).get("health_status", {}).get("entities", [])
            d["waba"] = next((str(e["id"]) for e in parts if e.get("entity_type") == "WABA" and e.get("id")), "")
        except RuntimeError:
            pass
    wa_save(d)
    wa_template(d)
    log(f"WhatsApp: connected, sending from {d['from']}")
    return wa_state()


def wa_account(waba):
    """The business account id, typed in by hand when Meta would not tell which one the number belongs to."""
    d, waba = wa_load(), re.sub(r"\D", "", str(waba or ""))
    if not d.get("token"):
        raise RuntimeError("WhatsApp is not connected yet.")
    if not waba:
        raise RuntimeError("The WhatsApp Business Account ID is a long number.")
    try:
        nums = wa_call(d["token"], f"{waba}/phone_numbers", {"fields": "id,display_phone_number"}).get("data", [])
    except RuntimeError as ex:
        if "nonexisting field" in str(ex) or "does not exist" in str(ex):
            raise RuntimeError("That is not a WhatsApp Business Account ID. It may be the app ID or the phone number ID; "
                               "the account ID is a different long number.") from None
        raise RuntimeError(f"That account ID did not work: {ex}.") from None
    if d["phone_id"] not in [str(n.get("id")) for n in nums]:
        raise RuntimeError("That account exists but your sending number is not in it. Check that it is the WhatsApp Business Account ID, not the app ID or the phone number ID.")
    d["waba"] = waba
    wa_save(d)
    wa_template(d)
    return wa_state()


def wa_off():
    WA_FILE.unlink(missing_ok=True)
    r, a = recipients(), recipients(active=True)
    if not a["phones"] and not (email_load().get("on") and a["emails"]):       # never leave alerts with nowhere to go
        if PHONE:
            recipients_save([PHONE], r["emails"], r["sms"], r["whatsapp"], r["paused"])
    log("WhatsApp: switched off")
    return wa_state()


def wa_send(d, text, image, numbers):
    """Send to each number. Returns the numbers Meta accepted the message for."""
    if d.get("tpl_status") != "APPROVED" and time.time() - d.get("tpl_checked", 0) > 300:
        wa_template(d)                                    # has Meta approved the template by now?
    approved = d.get("template") and d.get("tpl_status") == "APPROVED"
    if d.get("expires") and 0 < d["expires"] - time.time() < 3 * 86400 and time.time() - d.get("warned", 0) > 86400:
        log(f"WhatsApp: the access token runs out on {dt.datetime.fromtimestamp(d['expires']):%-d %b}. Make a new one on Meta's page and connect again.")
        d["warned"] = time.time()
        wa_save(d)
    media = None
    if image and Path(image).exists():
        try:
            media = wa_call(d["token"], f"{d['phone_id']}/media", method="POST", upload=image, timeout=60,
                            fields={"messaging_product": "whatsapp", "type": "image/jpeg"}).get("id")
        except RuntimeError as ex:
            log(f"WhatsApp: the picture could not be uploaded ({ex})")
    body = " ".join(text.split())
    short = re.sub(r"^CON ALERT:\s*", "", body).rstrip(".")[:900]
    ok, results = [], {}
    for n in numbers:
        msg = {"messaging_product": "whatsapp", "to": wa_number(n).lstrip("+")}
        try:
            if approved:
                wa_call(d["token"], f"{d['phone_id']}/messages", method="POST", fields={**msg, "type": "template", "template": {
                    "name": d["template"], "language": {"code": d.get("lang", "en_US")},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": short}]}]}})
                if media:                                 # only arrives if they wrote to the number in the last 24 hours
                    try:
                        wa_call(d["token"], f"{d['phone_id']}/messages", method="POST", fields={**msg, "type": "image", "image": {"id": media}})
                    except RuntimeError:
                        pass
            elif media:
                wa_call(d["token"], f"{d['phone_id']}/messages", method="POST", fields={**msg, "type": "image", "image": {"id": media, "caption": body[:1000]}})
            else:
                wa_call(d["token"], f"{d['phone_id']}/messages", method="POST", fields={**msg, "type": "text", "text": {"body": body[:4000]}})
            ok.append(n)
            results[f"wa:{n}"] = {"state": "ok", "why": "", "how": "WhatsApp", "at": f"{dt.datetime.now():%H:%M}"}
        except RuntimeError as ex:
            log(f"WHATSAPP FAILED to {n}: {ex}")
            results[f"wa:{n}"] = {"state": "failed", "why": str(ex), "how": "WhatsApp", "at": f"{dt.datetime.now():%H:%M}"}
    try:
        STATE.mkdir(exist_ok=True)
        DELIVERY.write_text(json.dumps({**_read_json(DELIVERY, {}), **results}))
    except Exception:
        pass
    return ok


# -------------------------------------------------------------- recipients
# Who gets each alert: recipients.json = {"phones": [...], "sms": [...], "emails": [...]}. Numbers (or
# Apple ID addresses) get an iMessage, except the numbers also listed under "sms": those get a plain
# carrier text through the iPhone (for phones without iMessage; no picture). Email addresses get an email
# once the email account above is switched on.
# Without the file it is just the PHONE setting at the top, as before.
RECIPIENTS_FILE = ROOT / "recipients.json"
ADDRESS = r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+"


def recipients(active=False):
    """The lists as saved; with active=True, without the entries that are switched off."""
    try:
        d = json.loads(RECIPIENTS_FILE.read_text())
    except Exception:
        d = {}
    phones = d.get("phones") if isinstance(d.get("phones"), list) else ([PHONE] if PHONE else [])
    emails = d.get("emails") if isinstance(d.get("emails"), list) else []
    sms = d.get("sms") if isinstance(d.get("sms"), list) else []
    wa = d.get("whatsapp") if isinstance(d.get("whatsapp"), list) else []
    paused = d.get("paused") if isinstance(d.get("paused"), dict) else {}
    out = {"phones": [str(p) for p in phones], "sms": [str(p) for p in sms if p in phones], "emails": [str(e) for e in emails],
           "whatsapp": [str(p) for p in wa],
           "paused": {k: [str(x) for x in paused.get(k, []) if x in lst] for k, lst in (("phones", phones), ("emails", emails), ("whatsapp", wa))}}
    if active:
        for k in ("phones", "emails", "whatsapp"):
            out[k] = [x for x in out[k] if x not in out["paused"][k]]
        out["sms"] = [x for x in out["sms"] if x in out["phones"]]
    return out


def recipients_save(phones, emails, sms=None, whatsapp=None, paused=None):
    """Check and store both lists. There must always be somewhere for an alert to go."""
    clean_p, clean_e = [], []
    for p in phones or []:
        p = str(p).strip()
        if not re.fullmatch(ADDRESS, p):                  # a phone number: keep + and digits
            digits = re.sub(r"[\s().-]", "", p)
            if not re.fullmatch(r"\+?\d{8,15}", digits):
                raise RuntimeError(f'"{p}" is not a phone number. Write it with the country code, like +61 400 000 000.')
            p = digits
        if p not in clean_p:
            clean_p.append(p)
    for e in emails or []:
        e = str(e).strip()
        if not re.fullmatch(ADDRESS, e):
            raise RuntimeError(f'"{e}" is not an email address.')
        if e.lower() not in [x.lower() for x in clean_e]:
            clean_e.append(e)
    clean_w = []
    for p in whatsapp or []:
        digits = re.sub(r"[\s().-]", "", str(p))
        if not re.fullmatch(r"\+?\d{8,15}", digits):
            raise RuntimeError(f'"{p}" is not a phone number. Write it with the country code, like +61 400 000 000.')
        digits = wa_number(digits)
        if digits not in clean_w:
            clean_w.append(digits)
    if len(clean_p) > 20 or len(clean_e) > 20 or len(clean_w) > 20:
        raise RuntimeError("That is too many: at most 20 of each.")
    paused = paused if isinstance(paused, dict) else {}
    norm = lambda p: re.sub(r"[\s().-]", "", str(p))
    off = {"phones": [p for p in clean_p if p in [norm(x) if "@" not in str(x) else str(x).strip() for x in paused.get("phones", [])]],
           "emails": [e for e in clean_e if e.lower() in [str(x).strip().lower() for x in paused.get("emails", [])]],
           "whatsapp": [p for p in clean_w if p in [wa_number(norm(x)) for x in paused.get("whatsapp", [])]]}
    on_p, on_e, on_w = ([x for x in lst if x not in off[k]] for k, lst in (("phones", clean_p), ("emails", clean_e), ("whatsapp", clean_w)))
    if not on_p and not (email_load().get("on") and on_e) and not (wa_load().get("token") and on_w):
        raise RuntimeError("Keep at least one number or address switched on, or alerts would have nowhere to go.")
    as_sms = [re.sub(r"[\s().-]", "", str(p)) for p in sms or []]
    as_sms = [p for p in clean_p if p in as_sms and "@" not in p]          # only numbers on the list can be SMS
    tmp = RECIPIENTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"phones": clean_p, "sms": as_sms, "emails": clean_e, "whatsapp": clean_w, "paused": off}, indent=2))
    tmp.replace(RECIPIENTS_FILE)
    return recipients()


def sends_to():
    r, parts = recipients(active=True), []
    im, sm = [p for p in r["phones"] if p not in r["sms"]], r["sms"]
    if im:
        parts.append(f"iMessage to {im[0]}" if len(im) == 1 else f"iMessage to {len(im)} numbers")
    if sm:
        parts.append(f"SMS to {sm[0]}" if len(sm) == 1 else f"SMS to {len(sm)} numbers")
    if email_load().get("on") and r["emails"]:
        parts.append(f"email to {r['emails'][0]}" if len(r["emails"]) == 1 else f"email to {len(r['emails'])} addresses")
    if wa_load().get("token") and r["whatsapp"]:
        parts.append(f"WhatsApp to {len(r['whatsapp'])} number{'s' if len(r['whatsapp']) > 1 else ''}")
    return " and ".join(parts) or (f"iMessage to {PHONE}" if PHONE else "nowhere yet (add a number or email in Settings)")


# Messages accepts a text even when it then fails to go out, so "sent" is checked afterwards: each text is
# noted in state/delivery-pending.json, and a little later the monitor looks in the Mac's own Messages
# records to see whether it really left (SMS) or was delivered (iMessage). The outcome per number is kept
# in state/delivery.json for the page.
DELIVERY_PENDING = STATE / "delivery-pending.json"
DELIVERY = STATE / "delivery.json"
CHAT_DB = Path.home() / "Library/Messages/chat.db"


def note_sent(to, sms):
    pending = _read_json(DELIVERY_PENDING, [])
    pending.append({"to": to, "sms": bool(sms), "at": time.time()})
    STATE.mkdir(exist_ok=True)
    DELIVERY_PENDING.write_text(json.dumps(pending[-60:]))


def check_deliveries(wait=45):
    """Look up texts sent at least `wait` seconds ago. Logs the ones that did not get through."""
    pending = _read_json(DELIVERY_PENDING, [])
    due = [p for p in pending if time.time() - p["at"] >= wait]
    if not due:
        return
    import sqlite3
    results = _read_json(DELIVERY, {})
    try:
        db = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)
    except Exception:
        db = None
    for p in due:
        rows = []
        if db:
            try:
                rows = db.execute(
                    "select m.service, m.is_sent, m.is_delivered, m.error from message m join handle h on h.rowid = m.handle_id "
                    "where m.is_from_me = 1 and (h.id = ? or h.id like ?) and m.date / 1000000000 + 978307200 >= ?",
                    (p["to"], "%" + re.sub(r"\D", "", p["to"])[-9:] if "@" not in p["to"] else p["to"], p["at"] - 3)).fetchall()
                # (Messages stores numbers with the country code, so a number typed as 04... is matched by its last digits)
            except Exception:
                rows = []
        how = "SMS" if p["sms"] else "iMessage"
        if not rows:
            continue                                      # cannot tell (no access to the records): say nothing
        if any(r[3] for r in rows):
            state, why = "failed", f"Messages could not send it (error {next(r[3] for r in rows if r[3])})"
        elif p["sms"]:
            state, why = ("ok", "") if all(r[1] for r in rows) else ("failed", "it never left the Mac")
        else:
            state, why = ("ok", "") if all(r[2] for r in rows) else ("unconfirmed", "not delivered yet: the phone may be off, or it has no iMessage")
        if state != "ok" and results.get(p["to"], {}).get("state") != state:
            log(f"NOT DELIVERED: {how} to {p['to']}: {why}")
        results[p["to"]] = {"state": state, "why": why, "how": how, "at": f"{dt.datetime.fromtimestamp(p['at']):%H:%M}"}
    if db:
        db.close()
    DELIVERY.write_text(json.dumps(results))
    DELIVERY_PENDING.write_text(json.dumps([p for p in _read_json(DELIVERY_PENDING, []) if p not in due]))


def imessage(to, text, image=None, sms=False):
    """A text through the Mac's Messages app: an iMessage (with the picture), or a plain carrier SMS."""
    args = ["osascript", "-e", SEND, to, text, "SMS" if sms else "iMessage"]
    if image and not sms:
        image = Path(image)
        if image.parent != ALERTS:              # Messages cannot read files outside ~/Pictures
            ALERTS.mkdir(parents=True, exist_ok=True)
            image = Path(shutil.copy(image, ALERTS / image.name))
        args.append(str(image))
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        log(f"SEND FAILED ({'SMS' if sms else 'iMessage'} to {to}): {r.stderr.strip()}")
        return False
    note_sent(to, sms)
    return True


def send(text, image=None):
    """Send to everyone on both lists. True if at least one of them got it."""
    if DRY_RUN:
        log(f"[dry run] would send: {text}" + (f"  [{Path(image).name}]" if image else ""))
        return True
    r, em, done = recipients(active=True), email_load(), []
    pic = f"  [{Path(image).name}]" if image else ""
    if em.get("on") and r["emails"]:
        try:
            email_send(em, text, image, r["emails"])
            done.append(f"email to {len(r['emails'])}")
        except RuntimeError as ex:
            log(f"EMAIL FAILED ({ex})")
    wa = wa_load()
    if wa.get("token") and r["whatsapp"]:
        got = wa_send(wa, text, image, r["whatsapp"])
        if got:
            done.append(f"WhatsApp to {len(got)}")
    phones = list(r["phones"])
    if not done and not phones:                           # nothing else got through
        if not PHONE:
            log("nowhere to send this alert: add a number or email address on the page (Settings)")
            return False
        log(f"sending by iMessage to {PHONE} instead")
        phones = [PHONE]
    got = [p for p in phones if imessage(p, text, image, sms=p in r["sms"])]
    n_sms = len([p for p in got if p in r["sms"]])
    if len(got) - n_sms:
        done.append(f"iMessage to {len(got) - n_sms}")
    if n_sms:
        done.append(f"SMS to {n_sms}")
    if not done:
        return False
    log(f"sent ({', '.join(done)}): {text}{pic}")
    return True
