import os, ssl, json, time
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import paho.mqtt.client as mqtt

APP_API_KEY = os.getenv("API_KEY", "change_me")
MQTT_HOST   = os.getenv("MQTT_HOST")
MQTT_PORT   = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER   = os.getenv("MQTT_USERNAME")
MQTT_PASS   = os.getenv("MQTT_PASSWORD")
MQTT_TLS    = os.getenv("MQTT_TLS", "true").lower() == "true"
TOPIC       = os.getenv("PRINT_TOPIC", "print/tickets")

app = FastAPI(title="Printer API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
