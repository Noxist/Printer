import os, ssl, json, time, base64, uuid, io, hmac, hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, HTTPException, Form, UploadFile, File, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response, PlainTextResponse
from pydantic import BaseModel
import paho.mqtt.client as mqtt
from PIL import Image, ImageDraw, ImageFont

# ----------------- Konfiguration -----------------
APP_API_KEY = os.getenv("API_KEY", "change_me")
MQTT_HOST   = os.getenv("MQTT_HOST")
MQTT_PORT   = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER   = os.getenv("MQTT_USERNAME")
MQTT_PASS   = os.getenv("MQTT_PASSWORD")
MQTT_TLS    = os.getenv("MQTT_TLS", "true").lower() == "true"
TOPIC       = os.getenv("PRINT_TOPIC", "print/tickets")

UI_PASS     = os.getenv("UI_PASS", "set_me")
UI_REMEMBER_DAYS = int(os.getenv("UI_REMEMBER_DAYS", "30"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Zurich"))

# Druckbreite: 72mm * 8 dpmm = 576 px (Standard beim HS-830 lt. Manual)
PRINT_WIDTH_PX = int(os.getenv("PRINT_WIDTH_PX", "576"))

# ----------------- App & MQTT -----------------
app = FastAPI(title="Printer API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = mqtt.Client()
if MQTT_TLS:
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
client.username_pw_set(MQTT_USER, MQTT_PASS)
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_start()

# ----------------- Utils -----------------
def now_str():
    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M")

def make_ticket_id():
    return f"web-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"

def mqtt_publish_textlike(data: dict):
    """Dein bisheriges leichtes JSON-Schema (falls du später eine eigene Bridge nutzt)."""
    payload = {**data, "ts": int(time.time())}
    client.publish(TOPIC, json.dumps(payload), qos=1, retain=False)

def mqtt_publish_image_base64(b64_png: str, cut_paper: int = 1, paper_width_mm: int = 0, paper_height_mm: int = 0):
    """Offizielle JSON-Schnittstelle des HS-830 für Bilder/PDFs (Handbuch 8.6)."""
    payload = {
        "ticket_id": make_ticket_id(),
        "data_type": "png",
        "data_base64": b64_png,
        "paper_type": 0,
        "paper_width_mm": paper_width_mm,   # 0 = Drucker-Default (HS-830: 72mm print area)
        "paper_height_mm": paper_height_mm, # 0 = unbegrenzt (für Tickets)
        "cut_paper": cut_paper              # 1 = am Ende jeder Seite schneiden
    }
    client.publish(TOPIC, json.dumps(payload), qos=1, retain=False)

def pil_to_base64_png(img: Image.Image) -> str:
    buf = io.BytesIO()
    # 1-Bit Schwarz/Weiß mit Dithering → kleine Dateien, klare Kanten
    img = img.convert("1")
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")

def render_text_ticket(title: str, lines: list[str], add_datetime: bool = True) -> Image.Image:
    # Canvas in Weiß
    # Höhe dynamisch: grob 40px pro Zeile + Margin
    margin = 20
    line_h = 36
    font_title = ImageFont.load_default()
    font_body  = ImageFont.load_default()

    # optional bessere Fonts: via TTF auf den Drucker laden oder serverseitig bundlen
    # Beispiel: ImageFont.truetype("DejaVuSans.ttf", 28)

    text_lines = []
    if title.strip():
        text_lines.append(("__title__", title.strip()))
    text_lines += [("body", ln) for ln in lines if ln.strip()]
    if add_datetime:
        text_lines.append(("meta", f"{now_str()}"))

    h = margin*2 + line_h * len(text_lines)
    img = Image.new("L", (PRINT_WIDTH_PX, max(h, 120)), color=255)
    draw = ImageDraw.Draw(img)

    y = margin
    for kind, txt in text_lines:
        if kind == "__title__":
            draw.text((0, y), txt, font=font_title, fill=0)
        elif kind == "meta":
            draw.text((0, y), txt, font=font_body, fill=0)
        else:
            draw.text((0, y), txt, font=font_body, fill=0)
        y += line_h

    return img

# ----------------- Security (UI-Cookie) -----------------
COOKIE_NAME = "ui_token"

def sign_token(ts: str) -> str:
    sig = hmac.new(APP_API_KEY.encode(), ts.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{sig}"

def verify_token(token: str) -> bool:
    try:
        ts, sig = token.split(".")
        if sign_token(ts) != token:
            return False
        # Ablauf prüfen
        t = datetime.fromtimestamp(int(ts), tz=TZ)
        return datetime.now(TZ) - t < timedelta(days=UI_REMEMBER_DAYS)
    except Exception:
        return False

def require_ui_auth(request: Request):
    # 1) API Key im Header oder Query akzeptieren
    if (request.headers.get("x-api-key") or request.query_params.get("key")) == APP_API_KEY:
        return True
    # 2) Signiertes Cookie
    tok = request.cookies.get(COOKIE_NAME)
    if tok and verify_token(tok):
        return True
    return False

def check_api_key(req: Request):
    key = req.headers.get("x-api-key") or req.query_params.get("key")
    if key != APP_API_KEY:
        raise HTTPException(401, "invalid api key")

# ----------------- Schemas (programmatic) -----------------
class PrintPayload(BaseModel):
    title: str = "TASKS"
    lines: list[str] = []
    cut: bool = True
    add_datetime: bool = True

class RawPayload(BaseModel):
    text: str
    add_datetime: bool = False

# ----------------- API: Health -----------------
@app.get("/")
def ok():
    return {"ok": True, "topic": TOPIC}

# (deine alten Endpunkte bleiben)
@app.post("/print")
async def print_job(p: PrintPayload, request: Request):
    check_api_key(request)
    img = render_text_ticket(p.title, p.lines, add_datetime=p.add_datetime)
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=1)
    return {"ok": True}

@app.post("/webhook/print")
async def webhook(request: Request):
    check_api_key(request)
    data = await request.json() if "application/json" in (request.headers.get("content-type") or "") else {}
    text = data.get("text") or request.query_params.get("text")
    if not text:
        raise HTTPException(400, "text required")
    img = render_text_ticket("TASK", [text], add_datetime=True)
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=1)
    return {"ok": True}

# ----------------- API: Programmatisch pro Modus -----------------
@app.post("/api/print/template")
async def api_print_template(p: PrintPayload, request: Request):
    check_api_key(request)
    img = render_text_ticket(p.title, p.lines, add_datetime=p.add_datetime)
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=1)
    return {"ok": True}

@app.post("/api/print/raw")
async def api_print_raw(p: RawPayload, request: Request):
    check_api_key(request)
    lines = (p.text + (f"\n{now_str()}" if p.add_datetime else "")).splitlines()
    img = render_text_ticket("", lines, add_datetime=False)
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=1)
    return {"ok": True}

@app.post("/api/print/image")
async def api_print_image(request: Request, file: UploadFile = File(...)):
    check_api_key(request)
    content = await file.read()
    img = Image.open(io.BytesIO(content)).convert("L")
    # auf Druckbreite skalieren
    w, h = img.size
    if w != PRINT_WIDTH_PX:
        new_h = int(h * (PRINT_WIDTH_PX / w))
        img = img.resize((PRINT_WIDTH_PX, new_h))
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=1)
    return {"ok": True}

# ----------------- UI -----------------
HTML_PAGE = """
<!doctype html><meta charset="utf-8">
<title>Printer UI</title>
<style>
 body{font-family:system-ui;margin:2rem;max-width:720px}
 textarea,input[type=text],input[type=password]{width:100%;padding:.6rem;margin:.3rem 0}
 button{padding:.6rem 1rem;cursor:pointer}
 .row{display:flex;gap:10px;align-items:center}
 .tabs{display:flex;gap:8px;margin:.5rem 0}
 .tab button{padding:.4rem .8rem}
 .ok{background:#e6ffed;padding:.6rem;border-radius:.4rem;margin:.6rem 0}
 .err{background:#ffecec;padding:.6rem;border-radius:.4rem;margin:.6rem 0}
 .card{border:1px solid #eee;border-radius:10px;padding:12px;margin:12px 0}
 small{color:#666}
</style>
<h1>Quittungsdruck</h1>

<div class="tabs">
  <div class="tab"><button onclick="show('tpl')">Vorlage</button></div>
  <div class="tab"><button onclick="show('raw')">Raw Text</button></div>
  <div class="tab"><button onclick="show('img')">Bild</button></div>
</div>

<div id="msg">{{MSG}}</div>

<div id="pane_tpl" class="card">
  <form method="post" action="/ui/print/template">
    <label>Titel</label>
    <input type="text" name="title" value="MORGEN" />
    <label>Zeilen (eine pro Zeile)</label>
    <textarea name="lines" placeholder="Lesen – 10 Min&#10;Kaffee machen"></textarea>
    <div class="row">
      <label><input type="checkbox" name="add_dt" checked> Datum/Zeit automatisch</label>
      <span style="flex:1 1 auto"></span>
      <label>UI Passwort</label>
      <input type="password" name="pass" placeholder="UI Passwort" />
      <label class="row"><input type="checkbox" name="remember"> Angemeldet bleiben</label>
    </div>
    <button type="submit">Drucken</button>
  </form>
</div>

<div id="pane_raw" class="card" style="display:none">
  <form method="post" action="/ui/print/raw">
    <label>Freitext</label>
    <textarea name="text" placeholder="Irgendein kurzer Text..."></textarea>
    <div class="row">
      <label><input type="checkbox" name="add_dt"> Datum/Zeit anhängen</label>
      <span style="flex:1 1 auto"></span>
      <label>UI Passwort</label>
      <input type="password" name="pass" placeholder="UI Passwort" />
      <label class="row"><input type="checkbox" name="remember"> Angemeldet bleiben</label>
    </div>
    <button type="submit">Drucken</button>
  </form>
</div>

<div id="pane_img" class="card" style="display:none">
  <form method="post" action="/ui/print/image" enctype="multipart/form-data">
    <label>Bilddatei (PNG/JPG/PDF)</label>
    <input type="file" name="file" accept=".png,.jpg,.jpeg,.pdf" />
    <div class="row">
      <span style="flex:1 1 auto"></span>
      <label>UI Passwort</label>
      <input type="password" name="pass" placeholder="UI Passwort" />
      <label class="row"><input type="checkbox" name="remember"> Angemeldet bleiben</label>
    </div>
    <small>Hinweis: Datei wird in s/w umgewandelt und auf 576px Breite skaliert.</small><br>
    <button type="submit">Drucken</button>
  </form>
</div>

<script>
 function show(which){
   document.getElementById('pane_tpl').style.display = (which==='tpl')?'block':'none';
   document.getElementById('pane_raw').style.display = (which==='raw')?'block':'none';
   document.getElementById('pane_img').style.display = (which==='img')?'block':'none';
 }
</script>
"""

def page(msg=""):
    return HTMLResponse(HTML_PAGE.replace("{{MSG}}", msg))

@app.get("/ui", response_class=HTMLResponse)
def ui(request: Request):
    return page()

def _ui_auth_or_error(request: Request, pass_: str, remember: bool):
    if require_ui_auth(request):
        return True, None
    if pass_ == UI_PASS:
        # Remember: signiertes Cookie setzen
        if remember:
            ts = str(int(time.time()))
            token = sign_token(ts)
            r = RedirectResponse("/ui", status_code=303)
            r.set_cookie(COOKIE_NAME, token, max_age=UI_REMEMBER_DAYS*24*3600, httponly=True, samesite="lax")
            return True, r
        return True, None
    return False, page('<div class="err">Falsches Passwort</div>')

@app.post("/ui/print/template", response_class=HTMLResponse)
async def ui_print_template(
    request: Request,
    title: str = Form("TASKS"),
    lines: str = Form(""),
    add_dt: bool = Form(False),
    pass_: str = Form(..., alias="pass"),
    remember: bool = Form(False)
):
    ok, resp = _ui_auth_or_error(request, pass_, remember)
    if not ok:
        return resp
    img = render_text_ticket(title.strip(), [ln.strip() for ln in lines.splitlines()], add_datetime=add_dt)
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=1)
    return page('<div class="ok">Gesendet ✅</div>')

@app.post("/ui/print/raw", response_class=HTMLResponse)
async def ui_print_raw(
    request: Request,
    text: str = Form(""),
    add_dt: bool = Form(False),
    pass_: str = Form(..., alias="pass"),
    remember: bool = Form(False)
):
    ok, resp = _ui_auth_or_error(request, pass_, remember)
    if not ok:
        return resp
    lines = (text + (f"\n{now_str()}" if add_dt else "")).splitlines()
    img = render_text_ticket("", lines, add_datetime=False)
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=1)
    return page('<div class="ok">Gesendet ✅</div>')

@app.post("/ui/print/image", response_class=HTMLResponse)
async def ui_print_image(
    request: Request,
    file: UploadFile = File(...),
    pass_: str = Form(..., alias="pass"),
    remember: bool = Form(False)
):
    ok, resp = _ui_auth_or_error(request, pass_, remember)
    if not ok:
        return resp
    content = await file.read()
    # PDF-Unterstützung: Vereinfachung – hier nur PNG/JPG direkt.
    # (PDF → Bild könntest du später mit 'pdf2image' ergänzen.)
    img = Image.open(io.BytesIO(content)).convert("L")
    w, h = img.size
    if w != PRINT_WIDTH_PX:
        img = img.resize((PRINT_WIDTH_PX, int(h * (PRINT_WIDTH_PX / w))))
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=1)
    return page('<div class="ok">Gesendet ✅</div>')
