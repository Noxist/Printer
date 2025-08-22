import os, ssl, json, time
from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import paho.mqtt.client as mqtt

APP_API_KEY = os.getenv("API_KEY", "change_me")
MQTT_HOST   = os.getenv("MQTT_HOST")
MQTT_PORT   = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER   = os.getenv("MQTT_USERNAME")
MQTT_PASS   = os.getenv("MQTT_PASSWORD")
MQTT_TLS    = os.getenv("MQTT_TLS", "true").lower() == "true"
TOPIC       = os.getenv("PRINT_TOPIC", "print/tickets")
UI_PASS     = os.getenv("UI_PASS", "set_me")

app = FastAPI(title="Printer API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# MQTT Client
client = mqtt.Client()
if MQTT_TLS:
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
client.username_pw_set(MQTT_USER, MQTT_PASS)
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_start()

class PrintPayload(BaseModel):
    title: str = "TASKS"
    lines: list[str] = []
    cut: bool = True

def publish(data: dict):
    payload = {**data, "ts": int(time.time())}
    client.publish(TOPIC, json.dumps(payload), qos=1, retain=False)

def check_key(req: Request):
    key = req.headers.get("x-api-key") or req.query_params.get("key")
    if key != APP_API_KEY:
        raise HTTPException(401, "invalid api key")

@app.get("/")
def ok():
    return {"ok": True, "topic": TOPIC}

@app.post("/print")
async def print_job(p: PrintPayload, request: Request):
    check_key(request)
    publish({"type":"task", "title": p.title, "lines": p.lines, "cut": p.cut})
    return {"ok": True}

@app.post("/webhook/print")
async def webhook(request: Request):
    check_key(request)
    data = await request.json() if "application/json" in (request.headers.get("content-type") or "") else {}
    text = data.get("text") or request.query_params.get("text")
    if not text:
        raise HTTPException(400, "text required")
    publish({"type":"task", "title":"TASK", "lines":[text], "cut": True})
    return {"ok": True}

# --- Minimales UI ---
HTML_PAGE = """
<!doctype html><meta charset="utf-8">
<title>Printer UI</title>
<style>
 body{font-family:system-ui;margin:2rem;max-width:720px}
 textarea, input[type=text], input[type=password]{width:100%;padding:.6rem;margin:.3rem 0}
 button{padding:.6rem 1rem;cursor:pointer}
 .ok{background:#e6ffed;padding:.6rem;border-radius:.4rem;margin:.6rem 0}
 .err{background:#ffecec;padding:.6rem;border-radius:.4rem;margin:.6rem 0}
</style>
<h1>Quittungsdruck</h1>
<form method="post" action="/ui/print">
  <label>Titel</label>
  <input type="text" name="title" value="MORGEN" />
  <label>Zeilen (eine pro Zeile)</label>
  <textarea name="lines" placeholder="Lesen – 10 Min&#10;Kaffee machen"></textarea>
  <label>Passwort</label>
  <input type="password" name="pass" placeholder="UI Passwort" />
  <button type="submit">Drucken</button>
</form>
{{MSG}}
"""

def page(msg=""):
    return HTMLResponse(HTML_PAGE.replace("{{MSG}}", msg))

@app.get("/ui", response_class=HTMLResponse)
def ui():
    return page()

@app.post("/ui/print", response_class=HTMLResponse)
async def ui_print(title: str = Form("TASKS"), lines: str = Form(""), pass_: str = Form(..., alias="pass")):
    if pass_ != UI_PASS:
        return page('<div class="err">Falsches Passwort</div>')
    payload = {
        "type":"task",
        "title": title.strip() or "TASKS",
        "lines": [ln.strip() for ln in lines.splitlines() if ln.strip()],
        "cut": True
    }
    publish(payload)
    return page('<div class="ok">Gesendet ✅</div>')
