#!/usr/bin/env python3
"""J.A.R.V.I.S. — tu asistente personal 24/7. Un solo archivo. Licencia MIT."""
import json, os, sys, time, sqlite3, smtplib, threading, urllib.request, base64
from datetime import datetime, timedelta
from email.message import EmailMessage
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo
import anthropic
from anthropic import beta_tool

C = json.load(open(os.getenv("JARVIS_CONFIG", "config.json"), encoding="utf-8"))
TZ, LOCK = ZoneInfo(C.get("timezone", "Europe/Madrid")), threading.RLock()
os.makedirs(C.get("data_dir", "data"), exist_ok=True)
DB = sqlite3.connect(os.path.join(C.get("data_dir", "data"), "memory.db"), check_same_thread=False)
DB.executescript("""CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY, text TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY, due TEXT, text TEXT, repeat TEXT, done INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS chat(id INTEGER PRIMARY KEY, role TEXT, text TEXT, ts TEXT);""")
URL = os.getenv("JARVIS_BASE_URL") or C.get("base_url")  # vacío = Claude (de pago) · "http://localhost:11434" = Ollama (gratis, en tu PC)
AI = anthropic.Anthropic(api_key=C.get("api_key") or ("ollama" if URL else None), base_url=URL or None)
now = lambda: datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

def q(sql, *a):  # consulta segura entre hilos
    with LOCK: r = DB.execute(sql, a).fetchall(); DB.commit(); return r

def email(to, subject, body):
    s, m = C["email"], EmailMessage()
    m["From"], m["To"], m["Subject"] = s["user"], to, subject; m.set_content(body)
    with (smtplib.SMTP_SSL if s.get("port", 465) == 465 else smtplib.SMTP)(s["host"], s.get("port", 465)) as c:
        if s.get("port", 465) != 465: c.starttls()
        c.login(s["user"], s["password"]); c.send_message(m)

def tg(chat, text):
    api("sendMessage", chat_id=chat, text=text[:4000])

def api(method, **p):
    req = urllib.request.Request(f"https://api.telegram.org/bot{C['telegram']['token']}/{method}",
                                 json.dumps(p).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=70))

def notify(subject, text):  # avisa por todos los canales configurados
    print(f"[{now()}] 🔔 {subject}: {text}", flush=True)
    if C.get("email", {}).get("host"): email(C["owner_email"], f"{C['name']}: {subject}", text)
    for chat in C.get("telegram", {}).get("allowed_chats", []): tg(chat, f"🔔 {subject}\n{text}")

# ── Herramientas que Jarvis puede usar por sí mismo ──
@beta_tool
def remember(fact: str) -> str:
    """Save an important long-term fact about the user (preferences, people, dates, goals).
    Args:
        fact: The fact to remember, written as a full sentence.
    """
    q("INSERT INTO facts(text, ts) VALUES(?,?)", fact, now()); return "Guardado."

@beta_tool
def forget(fact_id: int) -> str:
    """Delete a remembered fact by its id.
    Args:
        fact_id: Id of the fact (shown as [#id] in memory).
    """
    q("DELETE FROM facts WHERE id=?", fact_id); return "Olvidado."

@beta_tool
def add_reminder(when: str, text: str, repeat: str = "") -> str:
    """Schedule a reminder/alert that will be sent to the user by email/Telegram at that time.
    Args:
        when: Local date and time, format 'YYYY-MM-DD HH:MM'.
        text: What to remind.
        repeat: '' (once), 'daily' or 'weekly'.
    """
    datetime.strptime(when, "%Y-%m-%d %H:%M")
    q("INSERT INTO reminders(due, text, repeat) VALUES(?,?,?)", when, text, repeat); return f"Programado para {when}."

@beta_tool
def cancel_reminder(reminder_id: int) -> str:
    """Cancel a pending reminder.
    Args:
        reminder_id: Id of the reminder.
    """
    q("UPDATE reminders SET done=1 WHERE id=?", reminder_id); return "Cancelado."

@beta_tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email on behalf of the user. Confirm with the user first unless they clearly asked for it.
    Args:
        to: Recipient email address (use the owner's address to email the user).
        subject: Subject line.
        body: Plain text body.
    """
    email(to, subject, body); return f"Email enviado a {to}."

TOOLS = [remember, forget, add_reminder, cancel_reminder, send_email] + ([] if URL else [{"type": "web_search_20260209", "name": "web_search"}])

def ask(msg):  # el cerebro: memoria + contexto + herramientas
    with LOCK:
        facts = "\n".join(f"[#{i}] {t}" for i, t in q("SELECT id, text FROM facts")) or "(vacía)"
        rems = "\n".join(f"[#{i}] {d} {t} {r}" for i, d, t, r in q("SELECT id,due,text,repeat FROM reminders WHERE done=0 ORDER BY due")) or "(ninguno)"
        system = (C["personality"].format(**C) + f"\n\nResponde siempre en {C.get('language', 'español')}. "
                  f"Fecha y hora actual: {now()} ({TZ}). Email del usuario: {C.get('owner_email', '?')}.\n"
                  f"Usa 'remember' cuando aprendas algo importante del usuario.\n\nMEMORIA:\n{facts}\n\nRECORDATORIOS PENDIENTES:\n{rems}")
        hist = [{"role": r, "content": t} for r, t in q("SELECT role, text FROM (SELECT * FROM chat ORDER BY id DESC LIMIT ?) ORDER BY id", C.get("history", 30))]
        while hist and hist[0]["role"] != "user": hist.pop(0)
        model = C.get("model", "claude-opus-5")
        extra = {"fallbacks": "default", "betas": ["server-side-fallback-2026-07-01"]} if model.startswith(("claude-opus-5", "claude-fable")) else {}
        out = AI.beta.messages.tool_runner(model=model, max_tokens=16000, system=system, tools=TOOLS,
                                           messages=hist + [{"role": "user", "content": msg}], **extra).until_done()
        reply = "".join(b.text for b in out.content if b.type == "text").strip() or "…"
        q("INSERT INTO chat(role,text,ts) VALUES('user',?,?),('assistant',?,?)", msg, now(), reply, now())
        return reply

def scheduler():  # el vigilante: revisa recordatorios cada 30 s y envía el informe diario
    last_brief = now()[:10] if now()[11:] >= (C.get("briefing_time") or "") else None  # no repetir el informe al reiniciar
    while True:
        try:
            for i, due, text, rep in q("SELECT id,due,text,repeat FROM reminders WHERE done=0 AND due<=?", now()):
                nxt, step = datetime.strptime(due, "%Y-%m-%d %H:%M"), {"daily": timedelta(1), "weekly": timedelta(7)}.get(rep)
                while step and nxt.strftime("%Y-%m-%d %H:%M") <= now(): nxt += step  # salta a la próxima fecha futura
                nxt = step and nxt
                q("UPDATE reminders SET due=?, done=? WHERE id=?", nxt.strftime("%Y-%m-%d %H:%M") if nxt else due, 0 if nxt else 1, i)
                notify("Recordatorio", f"{text} ({due})")
            b = C.get("briefing_time")
            if b and now()[11:] >= b and last_brief != now()[:10]:
                last_brief = now()[:10]; notify("Informe del día", ask(C.get("briefing_prompt", "Dame mi informe del día: recordatorios de hoy y próximos, y lo que debo tener en cuenta.")))
        except Exception as e: print("Error en el vigilante:", e, flush=True)
        time.sleep(30)

def telegram():  # habla con Jarvis desde el móvil
    off, ok = 0, C["telegram"].get("allowed_chats", [])
    while True:
        try:
            for u in api("getUpdates", offset=off, timeout=60)["result"]:
                off, m = u["update_id"] + 1, u.get("message") or {}
                chat, text = m.get("chat", {}).get("id"), m.get("text")
                if not text: continue
                tg(chat, ask(text) if chat in ok else f"No autorizado. Tu chat id es {chat}: añádelo a 'allowed_chats' en config.json.")
        except Exception as e: print("Error Telegram:", e, flush=True); time.sleep(5)

def status():  # datos en vivo para el HUD
    return {"model": C.get("model", "claude-opus-5"), "free": bool(URL), "facts": q("SELECT COUNT(*) FROM facts")[0][0],
            "reminders": q("SELECT due, text, repeat FROM reminders WHERE done=0 ORDER BY due"),
            "chat": q("SELECT role, text FROM (SELECT * FROM chat ORDER BY id DESC LIMIT 20) ORDER BY id")}

PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), "rb").read()

class Web(BaseHTTPRequestHandler):  # interfaz web con voz
    def auth(self):
        if self.headers.get("Authorization") == "Basic " + base64.b64encode(f"jarvis:{C['web_password']}".encode()).decode(): return True
        self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Jarvis"'); self.end_headers()
    def send(self, body, ctype):
        self.send_response(200); self.send_header("Content-Type", ctype); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if not self.auth(): return
        if self.path == "/status": return self.send(json.dumps(status()).encode(), "application/json")
        self.send(PAGE.replace(b"{{NAME}}", C["name"].encode()), "text/html; charset=utf-8")
    def do_POST(self):
        if not self.auth(): return
        try: r = ask(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["msg"])
        except Exception as e: r = f"Error: {e}"
        self.send(json.dumps({"reply": r}).encode(), "application/json")
    def log_message(self, *a): pass

if __name__ == "__main__":
    if sys.argv[1:] == ["chat"]:  # modo terminal: python jarvis.py chat
        while True: print(f"\n{C['name']}:", ask(input("\nTú: ")))
    threading.Thread(target=scheduler, daemon=True).start()
    if C.get("telegram", {}).get("token"): threading.Thread(target=telegram, daemon=True).start()
    port = int(os.getenv("PORT", C.get("port", 8000)))
    print(f"{C['name']} en línea → http://localhost:{port}  (usuario: jarvis)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Web).serve_forever()
