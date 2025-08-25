# main.py
import os, ssl, json, time, base64, uuid, io, hmac, hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
PUBLISH_QOS = int(os.getenv("PRINT_QOS", "2"))   # <-- QoS konfigurierbar (Default 2)

UI_PASS = os.getenv("UI_PASS", "set_me")
COOKIE_NAME = "ui_token"
UI_REMEMBER_DAYS = int(os.getenv("UI_REMEMBER_DAYS", "30"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Zurich"))

# Druckbreite: 72mm * 8 dpmm = 576 px (HS-830 Standard)
PRINT_WIDTH_PX = int(os.getenv("PRINT_WIDTH_PX", "576"))

# ----------------- Fonts -----------------
FONT_TITLE = os.getenv("FONT_FILE_TITLE", "ttf/DejaVuSans-Bold.ttf")
FONT_BODY  = os.getenv("FONT_FILE_BODY", "ttf/DejaVuSans.ttf")
SIZE_TITLE = int(os.getenv("FONT_SIZE_TITLE", "36"))   # Titel-Schriftgrösse
SIZE_BODY  = int(os.getenv("FONT_SIZE_BODY", "28"))   # Text-Schriftgrösse

# ----------------- App & MQTT -----------------
app = FastAPI(title="Printer API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = mqtt.Client()
if MQTT_TLS:
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
if MQTT_USER or MQTT_PASS:
    client.username_pw_set(MQTT_USER, MQTT_PASS)
client.connect(MQTT_HOST, MQTT_PORT, 60)
client.loop_start()

from textwrap import wrap

MARGIN_X = 20   # linker & rechter Rand
MARGIN_Y = 20   # oberer Rand
SIZE_TITLE = 32
SIZE_BODY = 28
FONT_TITLE = "ttf/DejaVuSans-Bold.ttf"
FONT_BODY  = "ttf/DejaVuSans.ttf"

def render_text_ticket(title: str, lines: list[str], add_datetime: bool = True) -> Image.Image:
    font_title = ImageFont.truetype(FONT_TITLE, SIZE_TITLE)
    font_body  = ImageFont.truetype(FONT_BODY, SIZE_BODY)

    # Wrapping vorbereiten
    max_width = PRINT_WIDTH_PX - 2*MARGIN_X
    text_lines: list[tuple[str, str]] = []
    if title.strip():
        text_lines.append(("__title__", title.strip()))
    text_lines += [("body", ln) for ln in lines if ln.strip()]

    # Wrap jede Zeile
    wrapped_lines = []
    for kind, txt in text_lines:
        font = font_title if kind == "__title__" else font_body
        words = txt.split()
        line = ""
        for word in words:
            test = (line + " " + word).strip()
            w, _ = font.getsize(test)
            if w <= max_width:
                line = test
            else:
                wrapped_lines.append((kind, line))
                line = word
        if line:
            wrapped_lines.append((kind, line))

    # Höhe berechnen (Zeilenhöhe dynamisch)
    line_h = SIZE_BODY + 10
    h = max(MARGIN_Y*2 + line_h * len(wrapped_lines) + 50, 120)
    img = Image.new("L", (PRINT_WIDTH_PX, h), color=255)
    draw = ImageDraw.Draw(img)

    # Datum oben rechts
    if add_datetime:
        date_str = now_str()
        w, _ = font_body.getsize(date_str)
        draw.text((PRINT_WIDTH_PX - MARGIN_X - w, MARGIN_Y), date_str, font=font_body, fill=0)
        offset_y = MARGIN_Y + line_h  # Platz nach Datum
    else:
        offset_y = MARGIN_Y

    # Text zeichnen
    y = offset_y
    for kind, txt in wrapped_lines:
        font = font_title if kind == "__title__" else font_body
        draw.text((MARGIN_X, y), txt, font=font, fill=0)
        y += line_h

    return img



# ----------------- Security -----------------
def check_api_key(req: Request):
    key = req.headers.get("x-api-key") or req.query_params.get("key")
    if key != APP_API_KEY:
        raise HTTPException(401, "invalid api key")

def sign_token(ts: str) -> str:
    sig = hmac.new(APP_API_KEY.encode(), ts.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{sig}"

def verify_token(token: str) -> bool:
    try:
        ts, sig = token.split(".")
        if sign_token(ts) != token:
            return False
        created = datetime.fromtimestamp(int(ts), tz=TZ)
        return (datetime.now(TZ) - created) < timedelta(days=UI_REMEMBER_DAYS)
    except Exception:
        return False

def require_ui_auth(request: Request) -> bool:
    if (request.headers.get("x-api-key") or request.query_params.get("key")) == APP_API_KEY:
        return True
    tok = request.cookies.get(COOKIE_NAME)
    return bool(tok and verify_token(tok))

def issue_cookie(resp: Response):
    ts = str(int(time.time()))
    token = sign_token(ts)
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=UI_REMEMBER_DAYS * 24 * 3600,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/"
    )

def ui_auth_state(request: Request, pass_: str | None, remember: bool) -> tuple[bool, bool]:
    if require_ui_auth(request):
        return True, False
    if pass_ is not None and pass_ == UI_PASS:
        return True, bool(remember)
    return False, False

# ----------------- Schemas -----------------
class PrintPayload(BaseModel):
    title: str = "TASKS"
    lines: list[str] = []
    cut: bool = True
    add_datetime: bool = True

class RawPayload(BaseModel):
    text: str
    add_datetime: bool = False

# ----------------- API -----------------
@app.get("/")
def ok():
    return {"ok": True, "topic": TOPIC, "qos": PUBLISH_QOS}

@app.post("/print")
async def print_job(p: PrintPayload, request: Request):
    check_api_key(request)
    img = render_text_ticket(p.title, p.lines, add_datetime=p.add_datetime)
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=(1 if p.cut else 0))
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

@app.post("/api/print/template")
async def api_print_template(p: PrintPayload, request: Request):
    check_api_key(request)
    img = render_text_ticket(p.title, p.lines, add_datetime=p.add_datetime)
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=(1 if p.cut else 0))
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
    w, h = img.size
    if w != PRINT_WIDTH_PX:
        img = img.resize((PRINT_WIDTH_PX, int(h * (PRINT_WIDTH_PX / w))))
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=1)
    return {"ok": True}

# ----------------- UI -----------------
HTML_PAGE = """
<!doctype html><meta charset="utf-8">
<title>Printer UI</title>
<style>
 body{font-family:system-ui;margin:2rem;max-width:820px}
 textarea,input[type=text],input[type=password]{width:100%;padding:.6rem;margin:.3rem 0}
 button{padding:.6rem 1rem;cursor:pointer}
 .row{display:flex;gap:10px;align-items:center}
 .tabs{display:flex;gap:8px;margin:.5rem 0}
 .tab button{padding:.4rem .8rem}
 .ok{background:#e6ffed;padding:.6rem;border-radius:.4rem;margin:.6rem 0}
 .err{background:#ffecec;padding:.6rem;border-radius:.4rem;margin:.6rem 0}
 .card{border:1px solid #eee;border-radius:10px;padding:12px;margin:12px 0}
 small{color:#666}
 header{display:flex;gap:12px;align-items:center;margin-bottom:8px}
 header a{margin-left:auto;color:#666;text-decoration:none}
 header a:hover{text-decoration:underline}
</style>
<header>
  <h1>Quittungsdruck</h1>
  <a href="/ui/logout">Logout</a>
</header>

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
      <input type="password" name="pass" placeholder="falls noetig" />
      <label class="row"><input type="checkbox" name="remember"> Angemeldet bleiben</label>
    </div>
    <button type="submit">Drucken</button>
  </form>
</div>

<div id="pane_raw" class="card" style="display:none">
  <form method="post" action="/ui/print/raw">
    <label>Freitext</label>
    <textarea name="text" placeholder="Kurzer Notizzettel ..."></textarea>
    <div class="row">
      <label><input type="checkbox" name="add_dt"> Datum/Zeit anhaengen</label>
      <span style="flex:1 1 auto"></span>
      <label>UI Passwort</label>
      <input type="password" name="pass" placeholder="falls noetig" />
      <label class="row"><input type="checkbox" name="remember"> Angemeldet bleiben</label>
    </div>
    <button type="submit">Drucken</button>
  </form>
</div>

<div id="pane_img" class="card" style="display:none">
  <form method="post" action="/ui/print/image" enctype="multipart/form-data">
    <label>Bilddatei (PNG/JPG)</label>
    <input type="file" name="file" accept=".png,.jpg,.jpeg" />
    <div class="row">
      <span style="flex:1 1 auto"></span>
      <label>UI Passwort</label>
      <input type="password" name="pass" placeholder="falls noetig" />
      <label class="row"><input type="checkbox" name="remember"> Angemeldet bleiben</label>
    </div>
    <small>Bild wird in s/w konvertiert und auf {w}px Breite skaliert.</small><br>
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
""".replace("{w}", str(PRINT_WIDTH_PX))

def page(msg: str = "") -> HTMLResponse:
    return HTMLResponse(HTML_PAGE.replace("{{MSG}}", msg))

@app.get("/ui", response_class=HTMLResponse)
def ui(request: Request):
    msg = '<div class="ok">Angemeldet ✅ – Passwortfeld kann leer bleiben.</div>' if require_ui_auth(request) \
          else '<div class="err">Nicht angemeldet – Passwort einmal eingeben oder "angemeldet bleiben" waehlen.</div>'
    return page(msg)

@app.get("/ui/logout")
def ui_logout():
    r = RedirectResponse("/ui", status_code=303)
    r.delete_cookie(COOKIE_NAME, path="/")
    return r

def ui_handle_auth_and_cookie(request: Request, pass_: str | None, remember: bool) -> tuple[bool, bool]:
    authed, should_set_cookie = ui_auth_state(request, pass_, remember)
    if not authed:
        # sofort Fehlerseite liefern
        return False, False
    return True, should_set_cookie

# --------- UI: Drucken (Vorlage)
@app.post("/ui/print/template", response_class=HTMLResponse)
async def ui_print_template(
    request: Request,
    title: str = Form("TASKS"),
    lines: str = Form(""),
    add_dt: bool = Form(False),
    pass_: str | None = Form(None, alias="pass"),
    remember: bool = Form(False)
):
    authed, set_cookie = ui_handle_auth_and_cookie(request, pass_, remember)
    if not authed:
        return page('<div class="err">Falsches Passwort</div>')
    img = render_text_ticket(title.strip(), [ln.strip() for ln in lines.splitlines()], add_datetime=add_dt)
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=1)
    resp = page('<div class="ok">Gesendet ✅</div>')
    if set_cookie:
        issue_cookie(resp)
    return resp

# --------- UI: Drucken (Raw)
@app.post("/ui/print/raw", response_class=HTMLResponse)
async def ui_print_raw(
    request: Request,
    text: str = Form(""),
    add_dt: bool = Form(False),
    pass_: str | None = Form(None, alias="pass"),
    remember: bool = Form(False)
):
    authed, set_cookie = ui_handle_auth_and_cookie(request, pass_, remember)
    if not authed:
        return page('<div class="err">Falsches Passwort</div>')
    lines = (text + (f"\n{now_str()}" if add_dt else "")).splitlines()
    img = render_text_ticket("", lines, add_datetime=False)
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=1)
    resp = page('<div class="ok">Gesendet ✅</div>')
    if set_cookie:
        issue_cookie(resp)
    return resp

# --------- UI: Drucken (Bild)
@app.post("/ui/print/image", response_class=HTMLResponse)
async def ui_print_image(
    request: Request,
    file: UploadFile = File(...),
    pass_: str | None = Form(None, alias="pass"),
    remember: bool = Form(False)
):
    authed, set_cookie = ui_handle_auth_and_cookie(request, pass_, remember)
    if not authed:
        return page('<div class="err">Falsches Passwort</div>')
    content = await file.read()
    img = Image.open(io.BytesIO(content)).convert("L")
    w, h = img.size
    if w != PRINT_WIDTH_PX:
        img = img.resize((PRINT_WIDTH_PX, int(h * (PRINT_WIDTH_PX / w))))
    b64 = pil_to_base64_png(img)
    mqtt_publish_image_base64(b64, cut_paper=1)
    resp = page('<div class="ok">Gesendet ✅</div>')
    if set_cookie:
        issue_cookie(resp)
    return resp




