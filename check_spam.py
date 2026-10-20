#!/usr/bin/env python3
"""
Chequeo diario de spam para los dominios de envío de Central Ticket.

Fuentes:
  - Google Postmaster Tools API v1: spam rate reportado por usuarios de Gmail y
    reputación del dominio (los datos llegan con 1-2 días de demora).
  - Mailgun: tasa de quejas (complained / delivered) por dominio. No incluye a
    Gmail, que no informa quejas individuales a los proveedores de envío.

Credenciales (nunca se imprimen):
  - Mailgun: la agrega el proxy del entorno como network secret (Basic api:KEY).
    Si existe MAILGUN_API_KEY en el entorno, se usa esa en su lugar.
  - Google: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN.

Uso:
  python3 check_spam.py              # chequea y manda email solo si hay alertas
  python3 check_spam.py --dry-run    # chequea, no manda nada
  python3 check_spam.py --test-alert # manda un email de prueba aunque no haya alertas

Salida: un JSON en stdout con datos, alertas y si se envió el email.
Código de salida: 0 sin alertas, 1 con alertas, 2 error de configuración o de fuentes.
"""
import argparse
import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
POSTMASTER = "https://gmailpostmastertools.googleapis.com/v1"


def load_config():
    with open(os.path.join(HERE, "config.json")) as f:
        cfg = json.load(f)
    if os.environ.get("ALERT_FROM"):
        cfg["alert_from"] = os.environ["ALERT_FROM"]
    return cfg


def http(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read()[:500].decode("utf-8", "replace")
        return e.code, {"_error": body}


def mailgun_headers():
    key = os.environ.get("MAILGUN_API_KEY")
    if key:
        token = base64.b64encode(f"api:{key}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {}  # el proxy agrega la autenticación


# ---------------------------------------------------------------- Postmaster

def google_access_token():
    needed = ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"]
    missing = [v for v in needed if not os.environ.get(v)]
    if missing:
        return None, f"faltan variables de entorno: {', '.join(missing)}"
    data = urllib.parse.urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    status, body = http("POST", "https://oauth2.googleapis.com/token",
                        {"Content-Type": "application/x-www-form-urlencoded"}, data)
    if status != 200 or "access_token" not in body:
        return None, f"no se pudo renovar el token de Google (HTTP {status}): {body.get('_error', body.get('error', ''))}"
    return body["access_token"], None


def postmaster_domains(token):
    status, body = http("GET", f"{POSTMASTER}/domains?pageSize=100",
                        {"Authorization": f"Bearer {token}"})
    if status != 200:
        return None, f"Postmaster /domains HTTP {status}: {body.get('_error', '')}"
    return [d["name"].split("/", 1)[1] for d in body.get("domains", [])], None


def pick_postmaster_domain(domain, registered):
    """Usa el subdominio si está registrado; si no, el dominio padre más cercano."""
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in registered:
            return cand
    return None


def postmaster_stats(token, pm_domain, days):
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    q = {
        "pageSize": 100,
        "startDate.year": start.year, "startDate.month": start.month, "startDate.day": start.day,
        "endDate.year": end.year, "endDate.month": end.month, "endDate.day": end.day,
    }
    url = f"{POSTMASTER}/domains/{pm_domain}/trafficStats?{urllib.parse.urlencode(q)}"
    status, body = http("GET", url, {"Authorization": f"Bearer {token}"})
    if status != 200:
        return None, f"Postmaster trafficStats HTTP {status}: {body.get('_error', '')}"
    rows = []
    for s in body.get("trafficStats", []):
        day = s["name"].rsplit("/", 1)[-1]  # YYYYMMDD
        rows.append({
            "date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
            "spam_ratio": s.get("userReportedSpamRatio"),
            "reputation": s.get("domainReputation"),
        })
    rows.sort(key=lambda r: r["date"])
    return rows, None


# ------------------------------------------------------------------- Mailgun

def mailgun_stats(base, domain, days):
    q = urllib.parse.urlencode([("event", "delivered"), ("event", "complained"),
                                ("duration", f"{days}d"), ("resolution", "day")])
    status, body = http("GET", f"{base}/v3/{domain}/stats/total?{q}", mailgun_headers())
    if status != 200:
        return None, f"Mailgun stats HTTP {status}: {body.get('_error', '')}"
    rows = []
    for s in body.get("stats", []):
        t = dt.datetime.strptime(s["time"], "%a, %d %b %Y %H:%M:%S %Z").date().isoformat()
        delivered = (s.get("delivered") or {}).get("total", 0)
        complained = (s.get("complained") or {}).get("total", 0)
        rows.append({"date": t, "delivered": delivered, "complained": complained,
                     "complaint_rate": (complained / delivered) if delivered else None})
    rows.sort(key=lambda r: r["date"])
    return rows, None


def mailgun_send(base, sender, to, subject, text):
    domain = sender.split("@", 1)[1].strip(">").strip()
    data = urllib.parse.urlencode([("from", sender), ("subject", subject), ("text", text)]
                                  + [("to", t) for t in to]).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded", **mailgun_headers()}
    status, body = http("POST", f"{base}/v3/{domain}/messages", headers, data)
    return status == 200, f"HTTP {status} {body.get('message', body.get('_error', ''))}"


# ------------------------------------------------------------------ Reglas

def pct(x):
    return "sin dato" if x is None else f"{x * 100:.3f}%"


def evaluate(domain, pm_rows, mg_rows, th, pm_domain):
    alerts = []
    if pm_rows:
        valid = [r for r in pm_rows if r["spam_ratio"] is not None]
        if valid:
            last = valid[-1]
            prev = [r["spam_ratio"] for r in valid[:-1]][-7:]
            avg = sum(prev) / len(prev) if prev else None
            if last["spam_ratio"] >= th["gmail_spam_critical"]:
                alerts.append(("CRÍTICA", f"Spam rate Gmail {pct(last['spam_ratio'])} el {last['date']} (umbral crítico {pct(th['gmail_spam_critical'])}; Gmail empieza a penalizar)"))
            elif last["spam_ratio"] >= th["gmail_spam_warning"]:
                alerts.append(("ALERTA", f"Spam rate Gmail {pct(last['spam_ratio'])} el {last['date']} (por encima de {pct(th['gmail_spam_warning'])})"))
            if avg is not None and last["spam_ratio"] >= th["spike_min_ratio"] and last["spam_ratio"] > avg * th["spike_factor"]:
                alerts.append(("ALERTA", f"Suba brusca: {pct(last['spam_ratio'])} el {last['date']} vs. promedio previo {pct(avg)}"))
        last_rep = next((r for r in reversed(pm_rows) if r["reputation"]), None)
        if last_rep and last_rep["reputation"] in th["bad_reputations"]:
            alerts.append(("ALERTA", f"Reputación Postmaster {last_rep['reputation']} el {last_rep['date']}"))
        last_date = dt.date.fromisoformat(pm_rows[-1]["date"])
        if (dt.date.today() - last_date).days > th["postmaster_stale_days"]:
            alerts.append(("AVISO", f"Postmaster sin datos desde {pm_rows[-1]['date']} (volumen a Gmail bajo o problema de la fuente)"))
    if mg_rows:
        recent = [r for r in mg_rows if r["delivered"] >= th["mailgun_min_delivered"]]
        if recent and recent[-1]["complaint_rate"] is not None and recent[-1]["complaint_rate"] > th["mailgun_complaint_rate"]:
            r = recent[-1]
            alerts.append(("ALERTA", f"Quejas Mailgun {pct(r['complaint_rate'])} el {r['date']} ({r['complained']} de {r['delivered']} entregados)"))
    return alerts


def build_email(results, test):
    lines = []
    worst = "AVISO"
    for d in results:
        if not d["alerts"]:
            continue
        lines.append(f"== {d['domain']} ==")
        for level, msg in d["alerts"]:
            lines.append(f"[{level}] {msg}")
            if level == "CRÍTICA" or (level == "ALERTA" and worst == "AVISO"):
                worst = level
        lines.append("")
    if test and not lines:
        lines = ["Email de prueba: el chequeo corrió bien y no hay alertas."]
        worst = "PRUEBA"
    lines += ["", "Detalle de los últimos días:"]
    for d in results:
        lines.append(f"\n{d['domain']} (Postmaster: {d['postmaster_domain'] or 'no registrado'})")
        for r in (d["postmaster"] or [])[-5:]:
            lines.append(f"  Gmail {r['date']}: spam {pct(r['spam_ratio'])}, reputación {r['reputation'] or 'sin dato'}")
        for r in (d["mailgun"] or [])[-5:]:
            lines.append(f"  Mailgun {r['date']}: {r['delivered']} entregados, {r['complained']} quejas")
        for e in d["errors"]:
            lines.append(f"  ERROR: {e}")
    lines.append("\nNota: los datos de Gmail llegan con 1-2 días de demora.")
    subject = f"[{worst}] Spam rate dominios Central Ticket — {dt.date.today().isoformat()}"
    return subject, "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    th = cfg["thresholds"]
    out = {"date": dt.date.today().isoformat(), "global_errors": [], "domains": [], "email": None}

    token, err = google_access_token()
    registered = []
    if err:
        out["global_errors"].append(err)
    else:
        registered, err = postmaster_domains(token)
        if err:
            out["global_errors"].append(err)
            registered = []
    out["postmaster_registered"] = registered

    for domain in cfg["domains"]:
        d = {"domain": domain, "postmaster_domain": None, "postmaster": None, "mailgun": None, "errors": []}
        if token and registered is not None:
            pm = pick_postmaster_domain(domain, registered)
            d["postmaster_domain"] = pm
            if pm:
                d["postmaster"], e = postmaster_stats(token, pm, cfg["lookback_days"])
                if e:
                    d["errors"].append(e)
            elif registered:
                d["errors"].append("dominio no registrado en Postmaster")
        d["mailgun"], e = mailgun_stats(cfg["mailgun_api_base"], domain, cfg["lookback_days"])
        if e:
            d["errors"].append(e)
        d["alerts"] = evaluate(domain, d["postmaster"], d["mailgun"], th, d["postmaster_domain"])
        if d["errors"]:
            d["alerts"].append(("AVISO", "No se pudieron leer todas las fuentes: " + "; ".join(d["errors"])))
        out["domains"].append(d)

    for e in out["global_errors"]:
        for d in out["domains"]:
            d["alerts"].append(("AVISO", e))

    has_alerts = any(level in ("CRÍTICA", "ALERTA") for d in out["domains"] for level, _ in d["alerts"])
    has_warnings = any(d["alerts"] for d in out["domains"])
    should_send = has_alerts or has_warnings or args.test_alert

    if should_send and not args.dry_run:
        if not cfg.get("alert_from"):
            out["email"] = {"sent": False, "detail": "falta alert_from en config.json"}
        else:
            subject, text = build_email(out["domains"], args.test_alert)
            sender = cfg["alert_from"]
            # Si el dominio remitente es justo el que tiene problemas, la alerta podría caer en spam:
            # en ese caso se envía desde el remitente alternativo.
            sender_domain = sender.split("@", 1)[1].strip(">").strip()
            troubled = {d["domain"] for d in out["domains"]
                        if any(level in ("CRÍTICA", "ALERTA") for level, _ in d["alerts"])}
            if sender_domain in troubled and cfg.get("alert_from_fallback"):
                sender = cfg["alert_from_fallback"]
            ok, detail = mailgun_send(cfg["mailgun_api_base"], sender, cfg["alert_to"], subject, text)
            out["email"] = {"sent": ok, "detail": detail, "subject": subject, "from": sender, "to": cfg["alert_to"]}
    else:
        out["email"] = {"sent": False, "detail": "dry-run" if args.dry_run else "sin alertas"}

    print(json.dumps(out, ensure_ascii=False, indent=2))
    if out["global_errors"] or any(d["errors"] for d in out["domains"]):
        sys.exit(2)
    sys.exit(1 if has_alerts else 0)


if __name__ == "__main__":
    main()
