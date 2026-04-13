"""
TABUNG SIAGA GEMPA - Server Backend
FastAPI + SQLite + WebSocket + Serial Reader + Notifikasi Peringatan Dini

Instalasi:
    pip install fastapi uvicorn pyserial websockets

Cara pakai:
    1. Colok Arduino ke PC
    2. Cek port: Windows=COM3/COM4, Linux=/dev/ttyUSB0, Mac=/dev/tty.usbmodem*
    3. Ubah SERIAL_PORT di bawah sesuai port kamu
    4. Jalankan: python server.py
    5. Buka browser: http://localhost:8000
    6. Untuk akses publik (Mac): brew install ngrok → ngrok http 8000
"""

import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    serial = None
    SERIAL_AVAILABLE = False
    print("[WARN] pyserial tidak tersedia, mode simulasi aktif.")
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse

# ─── KONFIGURASI ─────────────────────────────────────────────────────────────

SERIAL_PORT  = "COM3"       # Windows: COM3/COM4 | Linux: /dev/ttyUSB0 | Mac: /dev/tty.usbmodem*
BAUD_RATE    = 9600
DB_FILE      = "gempa.db"

# Threshold sesuai flowchart (dalam satuan g, 1g = 16384 raw MPU6050 @ ±2g)
# magnitude = (abs(ax)+abs(ay)+abs(az)) / 16384
THRESHOLD_HIJAU   = 2.5   # < 2,5  → Aman (Hijau) - tidak dirasakan
THRESHOLD_KUNING  = 5.5   # < 5,5  → Waspada (Kuning) - dirasakan, kerusakan minim
THRESHOLD_ORANYE  = 6.1   # < 6,1  → Siaga (Oranye) - kerusakan ringan hingga signifikan
# >= 6,1 → Bahaya (Merah) - gempa besar hingga hebat
# >= 0.30g → Bahaya (Merah)

# Jumlah tabung minimum yang harus terdeteksi agar peringatan dikirim
MIN_TABUNG_AKTIF  = 5

# ─── KLASIFIKASI SESUAI FLOWCHART ────────────────────────────────────────────

def klasifikasi(magnitude_g: float) -> dict:
    """
    Klasifikasi 4 level skala Richter (0-10):
      Hijau   < 2,5  → Aman (tidak dirasakan)
      Kuning  < 5,5  → Waspada (dirasakan, kerusakan minim)
      Oranye  < 7,0  → Siaga (kerusakan ringan hingga signifikan)
      Merah  >= 7,0  → Bahaya (gempa besar hingga hebat)
    """
    if magnitude_g < THRESHOLD_HIJAU:
        return {"kategori": "HIJAU",  "label": "Aman",   "peringatan": False}
    elif magnitude_g < THRESHOLD_KUNING:
        return {"kategori": "KUNING", "label": "Waspada","peringatan": False}
    elif magnitude_g < THRESHOLD_ORANYE:
        return {"kategori": "ORANYE", "label": "Siaga",  "peringatan": True}
    else:
        return {"kategori": "MERAH",  "label": "Bahaya", "peringatan": True}

# ─── DATABASE ─────────────────────────────────────────────────────────────────

def init_db():
    """Buat semua tabel jika belum ada."""
    conn = sqlite3.connect(DB_FILE)

    # Tabel data sensor utama
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sensor_data (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    TEXT    NOT NULL,
            tabung_id    TEXT    DEFAULT 'TABUNG-1',
            ax           INTEGER,
            ay           INTEGER,
            az           INTEGER,
            gx           INTEGER,
            gy           INTEGER,
            gz           INTEGER,
            magnitude_raw INTEGER,
            magnitude_g  REAL,
            kategori     TEXT,
            label        TEXT
        )
    """)

    # Tabel perangkat yang terdaftar untuk menerima notifikasi
    conn.execute("""
        CREATE TABLE IF NOT EXISTS perangkat (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            nama         TEXT    NOT NULL,
            token_notif  TEXT,
            latitude     REAL,
            longitude    REAL,
            aktif        INTEGER DEFAULT 1,
            terdaftar_at TEXT
        )
    """)

    # Tabel log peringatan yang sudah dikirim
    conn.execute("""
        CREATE TABLE IF NOT EXISTS peringatan_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    TEXT    NOT NULL,
            kategori     TEXT,
            magnitude_g  REAL,
            jumlah_tabung INTEGER,
            pesan        TEXT,
            terkirim_ke  INTEGER
        )
    """)

    conn.commit()
    conn.close()

def save_to_db(data: dict) -> dict:
    """Simpan data sensor, kembalikan data lengkap dengan klasifikasi."""
    mag_raw = data.get("mag", 0)
    mag_g   = round(mag_raw / 100.0, 2)   # konversi balik ke skala 0-10
    info    = klasifikasi(mag_g)

    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT INTO sensor_data
          (timestamp, tabung_id, ax, ay, az, gx, gy, gz, magnitude_raw, magnitude_g, kategori, label)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["timestamp"],
        data.get("tabung_id", "TABUNG-1"),
        data.get("ax", 0), data.get("ay", 0), data.get("az", 0),
        data.get("gx", 0), data.get("gy", 0), data.get("gz", 0),
        mag_raw, mag_g,
        info["kategori"], info["label"]
    ))
    conn.commit()
    conn.close()

    return {**data, "magnitude_g": mag_g, **info}

def get_recent(limit: int = 100):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM sensor_data ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]

def get_stats():
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("""
        SELECT
            COUNT(*)                                             as total,
            SUM(CASE WHEN kategori='MERAH'  THEN 1 ELSE 0 END) as merah,
            SUM(CASE WHEN kategori='ORANYE' THEN 1 ELSE 0 END) as oranye,
            SUM(CASE WHEN kategori='KUNING' THEN 1 ELSE 0 END) as kuning,
            MAX(magnitude_g)                                     as max_g,
            AVG(magnitude_g)                                     as avg_g
        FROM sensor_data
    """).fetchone()
    conn.close()
    return {
        "total":  row[0] or 0,
        "merah":  row[1] or 0,
        "oranye": row[2] or 0,
        "kuning": row[3] or 0,
        "max_g":  round(row[4] or 0, 4),
        "avg_g":  round(row[5] or 0, 4),
    }

def get_tabung_aktif_terakhir(detik: int = 5) -> int:
    """Hitung berapa tabung unik aktif dalam N detik terakhir."""
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("""
        SELECT COUNT(DISTINCT tabung_id) FROM sensor_data
        WHERE timestamp >= datetime('now', ?)
          AND kategori IN ('ORANYE','MERAH')
    """, (f"-{detik} seconds",)).fetchone()
    conn.close()
    return row[0] or 0

def log_peringatan(kategori: str, mag_g: float, jml_tabung: int, pesan: str, terkirim: int):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT INTO peringatan_log (timestamp, kategori, magnitude_g, jumlah_tabung, pesan, terkirim_ke)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (datetime.now().isoformat(), kategori, mag_g, jml_tabung, pesan, terkirim))
    conn.commit()
    conn.close()

def get_perangkat_aktif() -> list:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM perangkat WHERE aktif=1").fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ─── WEBSOCKET MANAGER ────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []
        # Perangkat masyarakat yang subscribe notifikasi via WebSocket
        self.subscribers: dict[str, WebSocket] = {}  # device_id → ws

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        print(f"[WS] Dashboard terhubung. Total: {len(self.active)}")

    async def connect_device(self, ws: WebSocket, device_id: str):
        await ws.accept()
        self.subscribers[device_id] = ws
        print(f"[WS] Perangkat {device_id} terdaftar. Total subscriber: {len(self.subscribers)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        for k, v in list(self.subscribers.items()):
            if v == ws:
                del self.subscribers[k]
        print(f"[WS] Terputus. Dashboard: {len(self.active)}, Subscriber: {len(self.subscribers)}")

    async def broadcast_dashboard(self, message: dict):
        """Kirim ke semua dashboard."""
        data = json.dumps(message)
        for ws in list(self.active):
            try:
                await ws.send_text(data)
            except Exception:
                if ws in self.active:
                    self.active.remove(ws)

    async def broadcast_peringatan(self, peringatan: dict):
        """Kirim peringatan ke semua perangkat subscriber."""
        data = json.dumps(peringatan)
        terkirim = 0
        for device_id, ws in list(self.subscribers.items()):
            try:
                await ws.send_text(data)
                terkirim += 1
                print(f"[Notif] Terkirim ke {device_id}")
            except Exception:
                del self.subscribers[device_id]
        return terkirim

manager = ConnectionManager()

# ─── SISTEM PERINGATAN DINI ───────────────────────────────────────────────────

# Cooldown agar tidak spam notifikasi (dalam detik)
COOLDOWN_NOTIF = 30
last_notif_time: float = 0.0

async def proses_peringatan(data: dict):
    global last_notif_time

    kategori  = data.get("kategori", "HIJAU")
    mag_g     = data.get("magnitude_g", 0.0)
    tabung_id = data.get("tabung_id", "TABUNG-1")

    # Hanya proses jika oranye atau merah
    if kategori not in ("ORANYE", "MERAH"):
        return

    # Cooldown check
    now = time.time()
    if now - last_notif_time < COOLDOWN_NOTIF:
        return

    last_notif_time = now

    # Cek tabung mana saja yang aktif ORANYE/MERAH dalam 3 detik terakhir
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute("""
        SELECT DISTINCT tabung_id FROM sensor_data
        WHERE timestamp >= datetime('now', '-3 seconds')
          AND kategori IN ('ORANYE','MERAH')
    """).fetchall()
    conn.close()

    tabung_aktif = [r[0] for r in rows]
    if tabung_id not in tabung_aktif:
        tabung_aktif.append(tabung_id)

    jml_tabung   = len(tabung_aktif)
    nama_tabung  = ", ".join(tabung_aktif)

    if jml_tabung == 1:
        pesan = f"[PERINGATAN] Getaran {kategori} terdeteksi dari {nama_tabung}. Magnitudo: {mag_g:.3f}"
    else:
        pesan = f"[PERINGATAN GEMPA] Getaran {kategori} terdeteksi dari {jml_tabung} tabung ({nama_tabung})! Magnitudo: {mag_g:.3f}. Segera lakukan tindakan pengamanan!"

    # Buat paket peringatan
    peringatan = {
        "type":          "PERINGATAN",
        "timestamp":     datetime.now().isoformat(),
        "kategori":      kategori,
        "label":         data.get("label", ""),
        "magnitude_g":   mag_g,
        "jumlah_tabung": jml_tabung,
        "tabung_aktif":  nama_tabung,
        "pesan":         pesan,
        "perintah":      "AKTIFKAN_ALARM" if kategori == "MERAH" else "STANDBY_ALARM",
    }

    await manager.broadcast_dashboard({"type": "peringatan", "payload": peringatan})
    terkirim = await manager.broadcast_peringatan(peringatan)
    log_peringatan(kategori, mag_g, jml_tabung, pesan, terkirim)
    print(f"[Peringatan] {pesan} | Terkirim ke {terkirim} perangkat")

# ─── SERIAL READER ────────────────────────────────────────────────────────────

latest_data = {}
broadcast_queue: asyncio.Queue = None

def auto_detect_port() -> str:
    if not SERIAL_AVAILABLE:
        return SERIAL_PORT
    ports = serial.tools.list_ports.comports()
    for p in ports:
        if any(k in p.description.lower() for k in ["arduino", "ch340", "cp210", "ftdi", "usb serial"]):
            print(f"[Serial] Port otomatis ditemukan: {p.device}")
            return p.device
    if ports:
        print(f"[Serial] Pakai port pertama: {ports[0].device}")
        return ports[0].device
    return SERIAL_PORT

def serial_reader(loop: asyncio.AbstractEventLoop):
    if not SERIAL_AVAILABLE:
        print("[Serial] Tidak ada hardware serial, thread dihentikan.")
        return
    port = auto_detect_port()
    while True:
        try:
            print(f"[Serial] Mencoba terhubung ke {port} @ {BAUD_RATE}...")
            ser = serial.Serial(port, BAUD_RATE, timeout=2)
            print(f"[Serial] Terhubung ke {port}")

            while True:
                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if not line or line.startswith("READY:") or line.startswith("ERROR:"):
                    if line:
                        print(f"[Arduino] {line}")
                    continue

                try:
                    raw   = json.loads(line)
                    raw["timestamp"] = datetime.now().isoformat()
                    data  = save_to_db(raw)   # simpan + klasifikasi

                    global latest_data
                    latest_data = data

                    asyncio.run_coroutine_threadsafe(
                        broadcast_queue.put(data), loop
                    )
                    print(f"[Data] {data.get('kategori')} | {data.get('magnitude_g')}g")

                except json.JSONDecodeError:
                    print(f"[Serial] Bukan JSON: {line}")

        except serial.SerialException as e:
            print(f"[Serial] Error: {e}. Coba lagi dalam 3 detik...")
            time.sleep(3)

# ─── FASTAPI APP ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global broadcast_queue
    broadcast_queue = asyncio.Queue()
    init_db()
    loop = asyncio.get_event_loop()

    t = threading.Thread(target=serial_reader, args=(loop,), daemon=True)
    t.start()

    async def broadcaster():
        while True:
            data = await broadcast_queue.get()
            # Kirim data ke dashboard
            await manager.broadcast_dashboard({"type": "data", "payload": data})
            # Proses peringatan jika perlu
            await proses_peringatan(data)

    asyncio.create_task(broadcaster())
    yield

app = FastAPI(title="Tabung Siaga Gempa", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


# ─── WebSocket DASHBOARD ──────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_dashboard(ws: WebSocket):
    await manager.connect(ws)
    history = get_recent(50)
    await ws.send_text(json.dumps({"type": "history", "payload": history}))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ─── WebSocket PERANGKAT MASYARAKAT ──────────────────────────────────────────

@app.websocket("/ws/perangkat/{device_id}")
async def ws_perangkat(ws: WebSocket, device_id: str):
    """
    HP/perangkat masyarakat terhubung ke sini untuk terima notifikasi.
    Contoh koneksi dari browser/app:
        ws://xxx.ngrok.io/ws/perangkat/HP-BUDI-001
    """
    await manager.connect_device(ws, device_id)
    await ws.send_text(json.dumps({
        "type": "TERDAFTAR",
        "pesan": f"Perangkat {device_id} terdaftar. Akan menerima peringatan gempa.",
        "timestamp": datetime.now().isoformat()
    }))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ─── REST API ─────────────────────────────────────────────────────────────────

@app.get("/api/data")
async def api_data(limit: int = 100):
    return {"data": get_recent(limit)}

@app.get("/api/stats")
async def api_stats():
    stats = get_stats()
    stats["subscriber_aktif"] = len(manager.subscribers)
    stats["dashboard_aktif"]  = len(manager.active)
    return stats

@app.get("/api/latest")
async def api_latest():
    return latest_data or {"status": "Menunggu data Arduino..."}

@app.get("/api/perangkat")
async def api_perangkat():
    return {
        "terdaftar": get_perangkat_aktif(),
        "online_sekarang": list(manager.subscribers.keys())
    }

@app.post("/api/perangkat/daftar")
async def daftar_perangkat(req: Request):
    """Daftarkan perangkat baru (nama, latitude, longitude opsional)."""
    body = await req.json()
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT INTO perangkat (nama, token_notif, latitude, longitude, terdaftar_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        body.get("nama", "Anonim"),
        body.get("token", ""),
        body.get("latitude"),
        body.get("longitude"),
        datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()
    return {"status": "ok", "pesan": f"Perangkat {body.get('nama')} terdaftar."}

@app.post("/api/sensor")
async def api_sensor(req: Request):
    """
    Terima data sensor dari laptop yang terhubung ke Arduino via USB.
    Dipanggil oleh script kirim_railway.py di laptop teman.
    """
    try:
        data = await req.json()
        if "timestamp" not in data:
            data["timestamp"] = datetime.now().isoformat()

        result = save_to_db(data)

        global latest_data
        latest_data = result

        await manager.broadcast_dashboard({"type": "data", "payload": result})
        await proses_peringatan(result)

        return {"status": "ok", "kategori": result.get("kategori"), "magnitude_g": result.get("magnitude_g")}
    except Exception as e:
        return {"status": "error", "pesan": str(e)}

@app.get("/api/peringatan")
async def api_peringatan(limit: int = 20):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM peringatan_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return {"data": [dict(r) for r in rows]}


# ─── DASHBOARD HTML ───────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tabung Siaga Gempa</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; }
  header { background: #1e293b; padding: 16px 24px; border-bottom: 1px solid #334155;
           display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  header h1 { font-size: 20px; font-weight: 600; }
  #koneksi { margin-left: auto; font-size: 13px; padding: 4px 12px;
             border-radius: 12px; background: #334155; }
  #koneksi.online  { background: #14532d; color: #86efac; }
  #koneksi.offline { background: #7f1d1d; color: #fca5a5; }

  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
          gap: 12px; padding: 16px 24px; }
  .card { background: #1e293b; border-radius: 12px; padding: 14px;
          border: 1px solid #334155; }
  .card .label { font-size: 11px; color: #94a3b8; margin-bottom: 4px; text-transform: uppercase; letter-spacing:.04em; }
  .card .value { font-size: 26px; font-weight: 700; }
  .card .sub   { font-size: 11px; color: #64748b; margin-top: 2px; }

  /* Status 4 warna sesuai flowchart */
  #status-box { margin: 0 24px 16px; padding: 18px 24px; border-radius: 12px;
                font-size: 22px; font-weight: 700; text-align: center;
                transition: all .4s; border: 2px solid transparent; }
  #status-box.HIJAU  { background: #14532d; color: #86efac; border-color: #16a34a; }
  #status-box.KUNING { background: #713f12; color: #fde68a; border-color: #ca8a04; }
  #status-box.ORANYE { background: #7c2d12; color: #fdba74; border-color: #ea580c;
                       animation: pulse .8s infinite alternate; }
  #status-box.MERAH  { background: #7f1d1d; color: #fca5a5; border-color: #dc2626;
                       animation: pulse .4s infinite alternate; }
  @keyframes pulse { to { opacity: .75; transform: scale(1.01); } }

  /* Legenda threshold */
  .legenda { display: flex; gap: 8px; padding: 0 24px 12px; flex-wrap: wrap; }
  .leg-item { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #94a3b8; }
  .leg-dot { width: 10px; height: 10px; border-radius: 50%; }

  .section-title { font-size: 11px; color: #64748b; font-weight: 600;
                   letter-spacing: .06em; padding: 12px 24px 6px; text-transform: uppercase; }
  #grafik { margin: 0 24px 12px; background: #1e293b; border-radius: 12px;
            padding: 16px; border: 1px solid #334155; height: 180px; overflow: hidden; }
  #grafik canvas { width: 100%; height: 100%; }

  /* Panel peringatan */
  #panel-peringatan { margin: 0 24px 12px; }
  .alert-card { padding: 12px 16px; border-radius: 10px; margin-bottom: 8px;
                border-left: 4px solid; font-size: 13px; line-height: 1.5; }
  .alert-card.ORANYE { background: #431407; border-color: #ea580c; color: #fdba74; }
  .alert-card.MERAH  { background: #450a0a; border-color: #dc2626; color: #fca5a5; }
  .alert-card .a-time { font-size: 11px; color: #94a3b8; margin-top: 4px; }

  #log { margin: 0 24px 24px; background: #1e293b; border-radius: 12px;
         border: 1px solid #334155; overflow: hidden; }
  #log table { width: 100%; border-collapse: collapse; font-size: 13px; }
  #log thead th { padding: 10px 12px; text-align: left; color: #64748b;
                  font-weight: 600; border-bottom: 1px solid #334155; font-size:12px; }
  #log tbody tr { border-bottom: 1px solid #162032; transition: background .15s; }
  #log tbody tr:hover { background: #263348; }
  #log tbody td { padding: 7px 12px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
           font-size: 11px; font-weight: 700; }
  .badge.HIJAU  { background: #14532d; color: #86efac; }
  .badge.KUNING { background: #713f12; color: #fde68a; }
  .badge.ORANYE { background: #7c2d12; color: #fdba74; }
  .badge.MERAH  { background: #7f1d1d; color: #fca5a5; }

  /* Info koneksi perangkat */
  #info-subscriber { margin: 0 24px 8px; background: #1e293b; border-radius: 10px;
                     padding: 10px 16px; font-size: 13px; color: #94a3b8;
                     border: 1px solid #334155; display:flex; gap:16px; flex-wrap:wrap; }
</style>
</head>
<body>
<header>
  <span style="font-size:22px">🌍</span>
  <h1>Tabung Siaga Gempa — Dashboard</h1>
  <div id="koneksi" class="offline">● Offline</div>
</header>

<div class="grid">
  <div class="card"><div class="label">Total Pembacaan</div><div class="value" id="v-total">—</div></div>
  <div class="card"><div class="label">Magnitudo</div><div class="value" id="v-mag">—</div><div class="sub">terkini</div></div>
  <div class="card"><div class="label">Siaga (Oranye)</div><div class="value" id="v-oranye" style="color:#fdba74">—</div></div>
  <div class="card"><div class="label">Bahaya (Merah)</div><div class="value" id="v-merah" style="color:#fca5a5">—</div></div>
  <div class="card"><div class="label">Maks Magnitudo</div><div class="value" id="v-maxg">—</div></div>
  <div class="card"><div class="label">Perangkat Online</div><div class="value" id="v-subscriber" style="color:#60a5fa">—</div></div>
</div>

<div id="status-box">— Menunggu Data Sensor —</div>

<div class="legenda">
  <div class="leg-item"><div class="leg-dot" style="background:#16a34a"></div> Hijau &lt;2,5 — Tidak Dirasakan</div>
  <div class="leg-item"><div class="leg-dot" style="background:#ca8a04"></div> Kuning 2,5–5,4 — Kerusakan Minim</div>
  <div class="leg-item"><div class="leg-dot" style="background:#ea580c"></div> Oranye 5,5–6,0 — Kerusakan Ringan–Signifikan</div>
  <div class="leg-item"><div class="leg-dot" style="background:#dc2626"></div> Merah ≥6,1 — Gempa Besar–Hebat</div>
</div>

<div class="section-title">Grafik Magnitudo Real-time (Skala 0–10)</div>
<div id="grafik"><canvas id="chart"></canvas></div>

<div class="section-title">Peringatan Terkirim</div>
<div id="panel-peringatan"><div style="color:#475569;font-size:13px;padding:4px 0">Belum ada peringatan.</div></div>

<div id="info-subscriber">
  <span>Perangkat terhubung: <b id="sub-list">—</b></span>
  <span style="color:#475569;font-size:12px">Koneksi perangkat: ws://[URL-ngrok]/ws/perangkat/[ID-DEVICE]</span>
</div>

<div class="section-title">Log Data Sensor</div>
<div id="log">
  <table>
    <thead><tr><th>Waktu</th><th>Magnitudo</th><th>AX</th><th>AY</th><th>AZ</th><th>Kategori</th></tr></thead>
    <tbody id="log-body"></tbody>
  </table>
</div>

<script>
const MAX_POINTS = 80;
const magHistory = [];

// ─── Grafik ───────────────────────────────────────────
const canvas = document.getElementById('chart');
const ctx = canvas.getContext('2d');

const THRESHOLDS = [
  { val: 0.01, color: '#ca8a04', label: '0.01g' },
  { val: 0.05, color: '#ea580c', label: '0.05g' },
  { val: 0.30, color: '#dc2626', label: '0.30g' },
];

function drawChart() {
  const W = canvas.parentElement.clientWidth - 32;
  const H = canvas.parentElement.clientHeight - 32;
  canvas.width = W; canvas.height = H;
  ctx.clearRect(0, 0, W, H);
  if (magHistory.length < 2) return;

  const max = Math.max(Math.max(...magHistory) * 1.2, 0.35);
  const step = W / (MAX_POINTS - 1);

  // Grid lines
  ctx.strokeStyle = '#1e293b'; ctx.lineWidth = 1;
  for (let i = 0; i <= 5; i++) {
    const y = H - (i / 5) * H;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }

  // Threshold lines
  THRESHOLDS.forEach(t => {
    const y = H - (t.val / max) * H;
    ctx.strokeStyle = t.color; ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.fillStyle = t.color; ctx.font = '10px system-ui';
    ctx.fillText(t.label, 4, y - 3);
  });
  ctx.setLineDash([]);

  // Area fill
  ctx.beginPath();
  magHistory.forEach((v, i) => {
    const x = (MAX_POINTS - magHistory.length + i) * step;
    const y = H - (v / max) * H;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.lineTo((MAX_POINTS - 1) * step, H);
  ctx.lineTo((MAX_POINTS - magHistory.length) * step, H);
  ctx.closePath();
  ctx.fillStyle = 'rgba(96,165,250,0.1)';
  ctx.fill();

  // Garis utama
  ctx.beginPath();
  ctx.strokeStyle = '#60a5fa'; ctx.lineWidth = 2;
  magHistory.forEach((v, i) => {
    const x = (MAX_POINTS - magHistory.length + i) * step;
    const y = H - (v / max) * H;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
}

// ─── Log ──────────────────────────────────────────────
function addLogRow(d) {
  const tbody = document.getElementById('log-body');
  const ts  = (d.timestamp || '').split('T')[1]?.split('.')[0] || '—';
  const mg  = d.magnitude_g != null ? d.magnitude_g.toFixed(4) : (d.magnitude != null ? (d.magnitude/16384).toFixed(4) : '—');
  const kat = d.kategori || d.status || '—';
  const tr  = document.createElement('tr');
  tr.innerHTML = `<td>${ts}</td><td>${mg}</td><td>${d.ax??'—'}</td><td>${d.ay??'—'}</td><td>${d.az??'—'}</td>
    <td><span class="badge ${kat}">${d.label || kat}</span></td>`;
  tbody.prepend(tr);
  while (tbody.rows.length > 60) tbody.deleteRow(tbody.rows.length - 1);
}

// ─── Update UI ────────────────────────────────────────
function updateUI(d) {
  const mg  = d.magnitude_g != null ? d.magnitude_g : (d.mag || 0) / 16384;
  const kat = d.kategori || 'HIJAU';
  document.getElementById('v-mag').textContent = mg.toFixed(2);
  const box = document.getElementById('status-box');
  box.textContent = `${d.label || kat}  (${mg.toFixed(2)})`;
  box.className = kat;
  magHistory.push(mg);
  if (magHistory.length > MAX_POINTS) magHistory.shift();
  drawChart();
  addLogRow(d);
}

function updateStats(s) {
  document.getElementById('v-total').textContent      = s.total   ?? '—';
  document.getElementById('v-oranye').textContent     = s.oranye  ?? '—';
  document.getElementById('v-merah').textContent      = s.merah   ?? '—';
  document.getElementById('v-maxg').textContent       = s.max_g   ?? '—';
  document.getElementById('v-subscriber').textContent = s.subscriber_aktif ?? '—';
}

// ─── Peringatan ───────────────────────────────────────
function tampilkanPeringatan(p) {
  const panel = document.getElementById('panel-peringatan');
  const ts    = p.timestamp?.split('T')[1]?.split('.')[0] || '';
  const div   = document.createElement('div');
  div.className = `alert-card ${p.kategori}`;
  div.innerHTML = `
    <strong>${p.kategori === 'MERAH' ? '🚨' : '⚠️'} ${p.pesan}</strong>
    <div class="a-time">${ts} — Dari ${p.jumlah_tabung} tabung | ${p.perintah}</div>`;
  panel.prepend(div);
  if (panel.children.length > 10) panel.removeChild(panel.lastChild);
  // Notifikasi browser
  if (Notification.permission === 'granted') {
    new Notification('Tabung Siaga Gempa', { body: p.pesan, icon: '' });
  }
}

// ─── WebSocket ────────────────────────────────────────
Notification.requestPermission();

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws    = new WebSocket(`${proto}://${location.host}/ws`);
  const badge = document.getElementById('koneksi');

  ws.onopen = () => { badge.textContent = '● Online'; badge.className = 'online'; };

  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'data') {
      updateUI(msg.payload);
      fetch('/api/stats').then(r=>r.json()).then(s => {
        updateStats(s);
        document.getElementById('sub-list').textContent =
          Object.keys(s).length ? (s.subscriber_aktif + ' perangkat') : '—';
      });
    } else if (msg.type === 'history') {
      msg.payload.forEach(d => { magHistory.push(d.magnitude_g || 0); addLogRow(d); });
      if (magHistory.length > MAX_POINTS) magHistory.splice(0, magHistory.length - MAX_POINTS);
      drawChart();
      fetch('/api/stats').then(r=>r.json()).then(updateStats);
    } else if (msg.type === 'peringatan') {
      tampilkanPeringatan(msg.payload);
    }
  };

  ws.onclose = () => {
    badge.textContent = '● Offline'; badge.className = 'offline';
    setTimeout(connect, 3000);
  };
}

connect();
window.addEventListener('resize', drawChart);
</script>
</body>
</html>"""


# ─── JALANKAN ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("""
╔══════════════════════════════════════════╗
║       TABUNG SIAGA GEMPA - SERVER        ║
╠══════════════════════════════════════════╣
║  Dashboard : http://localhost:8000       ║
║  API Data  : http://localhost:8000/api   ║
║                                          ║
║  Untuk akses publik:                     ║
║    1. Download ngrok: ngrok.com          ║
║    2. Jalankan: ngrok http 8000          ║
║    3. Pakai URL https://xxxx.ngrok.io    ║
╚══════════════════════════════════════════╝
    """)
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
