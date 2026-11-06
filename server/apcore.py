"""Ядро AP Mapper без веб-зависимостей: БД, разбор CSV, пересчёт позиций точек, определение положения.
Его используют и HTTP-сервер (app.py), и шлюз для трекеров (gateway.py)."""
import csv
import io
import math
import os
import re
import sqlite3
import threading
import time
from calendar import timegm
from contextlib import contextmanager
from typing import Dict, Optional

DB_PATH = os.environ.get("DB_PATH", "/data/ap.db")
MIN_OBS = int(os.environ.get("MIN_OBS", "2"))  # наблюдений, чтобы точка участвовала в locate
MAX_ACC_M = float(os.environ.get("MAX_ACC_M", "100"))  # наблюдения с худшей погрешностью GPS отбрасываем
STATIC_MAX_SPREAD_M = {"wifi": 400.0, "ble": 150.0}  # шире разброс = точка, скорее всего, подвижная
MAX_LOCATE_AP = 6
PATH_LOSS_N = 3.0  # вес = 1/d^2 при показателе затухания n -> 10^(rssi / (5 n))

BSSID_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
BAD_BSSID = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS obs(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    ssid TEXT, auth TEXT, channel INTEGER,
    rssi INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, alt REAL, acc REAL,
    ts INTEGER NOT NULL,
    upload_id INTEGER,
    source TEXT DEFAULT 'app',
    UNIQUE(kind, key, ts, lat, lon)
);
CREATE INDEX IF NOT EXISTS obs_key ON obs(kind, key);
CREATE TABLE IF NOT EXISTS aps(
    kind TEXT NOT NULL, key TEXT NOT NULL, ssid TEXT,
    lat REAL NOT NULL, lon REAL NOT NULL, spread_m REAL NOT NULL,
    n_obs INTEGER NOT NULL, first_seen INTEGER, last_seen INTEGER, is_static INTEGER NOT NULL,
    PRIMARY KEY(kind, key)
);
CREATE TABLE IF NOT EXISTS uploads(
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,
    bytes INTEGER, rows INTEGER, new_rows INTEGER, skipped INTEGER
);
CREATE TABLE IF NOT EXISTS wifi_eval(
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, imei TEXT,
    gps_lat REAL, gps_lon REAL, wifi_lat REAL, wifi_lon REAL,
    err_m REAL NOT NULL, est_acc_m REAL NOT NULL, used INTEGER, scanned INTEGER
);
"""

write_lock = threading.Lock()


# ---------- БД ----------

def connect() -> sqlite3.Connection:
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript(SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(obs)")}
        if "source" not in cols:  # база создана до появления шлюза
            con.execute("ALTER TABLE obs ADD COLUMN source TEXT DEFAULT 'app'")


# ---------- геометрия ----------

def dist_m(lat1, lon1, lat2, lon2) -> float:
    k = 111_320.0
    dx = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2)) * k
    dy = (lat2 - lat1) * k
    return math.hypot(dx, dy)


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Азимут из точки 1 в точку 2, градусы 0..360 (0 = север, 90 = восток)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def centroid(points):
    """points: [(weight, lat, lon)] -> (lat, lon). Для небольших областей, без перехода через 180°."""
    sw = sum(w for w, _, _ in points)
    return (sum(w * la for w, la, _ in points) / sw, sum(w * lo for w, _, lo in points) / sw)


def normalize_bssid(s: str) -> Optional[str]:
    """'AA-BB-CC-DD-EE-FF' / 'aabbccddeeff' -> 'aa:bb:cc:dd:ee:ff'; мусор -> None."""
    h = re.sub(r"[:\-.]", "", s.strip().lower())
    if not re.fullmatch(r"[0-9a-f]{12}", h):
        return None
    b = ":".join(h[i:i + 2] for i in range(0, 12, 2))
    return None if b in BAD_BSSID else b


# ---------- разбор CSV ----------

def _parse_ts(s: str) -> Optional[int]:
    try:
        return timegm(time.strptime(s.strip(), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def parse_rows(text: str):
    """Генерирует (kind, key, ssid, auth, channel, rssi, lat, lon, alt, acc, ts) или None для отбракованных строк."""
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row:
            continue
        if row[0].startswith("WigleWifi") or row[0] == "MAC":
            continue
        if len(row) < 11:
            yield None
            continue
        mac, name, auth, first, chan, rssi, lat, lon, alt, acc, typ = row[:11]
        try:
            rssi_i, lat_f, lon_f = int(rssi), float(lat), float(lon)
            alt_f = float(alt) if alt else 0.0
            acc_f = float(acc) if acc else 0.0
            chan_i = int(chan) if chan else 0
        except ValueError:
            yield None
            continue
        ts = _parse_ts(first)
        if ts is None or not (-127 <= rssi_i <= 0):
            yield None
            continue
        if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180) or (lat_f == 0 and lon_f == 0):
            yield None
            continue
        if acc_f > MAX_ACC_M:
            yield None
            continue
        typ = typ.strip().upper()
        if typ == "WIFI":
            bssid = mac.strip().lower()
            if not BSSID_RE.match(bssid) or bssid in BAD_BSSID:
                yield None
                continue
            if name.lower().endswith("_nomap"):
                yield None
                continue
            yield ("wifi", bssid, name, auth, chan_i, rssi_i, lat_f, lon_f, alt_f, acc_f, ts)
        elif typ == "BLE":
            # Принимаем только стационарные маяки; id маяка лежит в колонке имени.
            bid = name.strip().lower()
            if not (bid.startswith("ibeacon:") or bid.startswith("eddystone:")):
                yield None
                continue
            yield ("ble", bid, "", "", 0, rssi_i, lat_f, lon_f, alt_f, acc_f, ts)
        else:
            yield None


# ---------- пересчёт позиций ----------

def recompute(con: sqlite3.Connection, keys):
    for kind, key in keys:
        rows = con.execute(
            "SELECT rssi, lat, lon, ts, ssid FROM obs WHERE kind=? AND key=?", (kind, key)
        ).fetchall()
        if not rows:
            continue
        mx = max(r[0] for r in rows)
        pts = [(10 ** ((r[0] - mx) / 10.0), r[1], r[2]) for r in rows]
        lat, lon = centroid(pts)
        spread = max(dist_m(lat, lon, r[1], r[2]) for r in rows)
        ssid = next((r[4] for r in reversed(rows) if r[4]), "")
        con.execute(
            "INSERT OR REPLACE INTO aps(kind,key,ssid,lat,lon,spread_m,n_obs,first_seen,last_seen,is_static) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (kind, key, ssid, lat, lon, spread, len(rows),
             min(r[3] for r in rows), max(r[3] for r in rows),
             1 if spread <= STATIC_MAX_SPREAD_M[kind] else 0),
        )


def add_observations(con: sqlite3.Connection, batch, upload_id: Optional[int], source: str) -> int:
    """Кладёт наблюдения (кортежи как из parse_rows), пересчитывает затронутые точки, возвращает число новых."""
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO obs(kind,key,ssid,auth,channel,rssi,lat,lon,alt,acc,ts,upload_id,source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [r + (upload_id, source) for r in batch],
    )
    new_rows = con.total_changes - before
    recompute(con, {(r[0], r[1]) for r in batch})
    return new_rows


def process_upload(body: bytes) -> dict:
    text = body.decode("utf-8", errors="replace")
    total = skipped = 0
    batch = []
    for r in parse_rows(text):
        total += 1
        if r is None:
            skipped += 1
        else:
            batch.append(r)
    with write_lock, db() as con:
        up_id = con.execute(
            "INSERT INTO uploads(ts,bytes,rows,new_rows,skipped) VALUES(?,?,?,?,?)",
            (int(time.time()), len(body), total, 0, skipped),
        ).lastrowid
        new_rows = add_observations(con, batch, up_id, "app")
        con.execute("UPDATE uploads SET new_rows=? WHERE id=?", (new_rows, up_id))
    return {"upload_id": up_id, "rows": total, "new": new_rows, "skipped": skipped}


def add_tracker_observations(rows) -> int:
    """Наблюдения от шлюза трекеров (там, где у трекера был нормальный GPS)."""
    with write_lock, db() as con:
        return add_observations(con, rows, None, "tracker")


# ---------- определение положения ----------

def locate_scan(scan: Dict[str, int]) -> Optional[dict]:
    """scan: {bssid или id маяка: rssi}. Возвращает {lat, lon, accuracy_m, used, scanned} или None."""
    ids: Dict[str, int] = {}
    for k, r in scan.items():
        kk = k.strip().lower()
        if kk not in ids or r > ids[kk]:
            ids[kk] = r
    if not ids:
        return None

    with db() as con:
        marks = ",".join("?" * len(ids))
        found = con.execute(
            f"SELECT key, lat, lon, spread_m FROM aps WHERE is_static=1 AND n_obs>=? AND key IN ({marks})",
            (MIN_OBS, *ids.keys()),
        ).fetchall()
    if not found:
        return None

    used = sorted(found, key=lambda r: ids[r[0]], reverse=True)[:MAX_LOCATE_AP]
    pts = [(10 ** (ids[k] / (5.0 * PATH_LOSS_N)), la, lo) for k, la, lo, _ in used]
    lat, lon = centroid(pts)
    if len(used) == 1:
        acc = max(50.0, used[0][3])
    else:
        sw = sum(w for w, _, _ in pts)
        acc = math.sqrt(sum(w * dist_m(lat, lon, la, lo) ** 2 for w, la, lo in pts) / sw)
        acc = max(15.0, acc)
    return {"lat": lat, "lon": lon, "accuracy_m": round(acc, 1), "used": len(used), "scanned": len(ids)}


# ---------- сравнение Wi-Fi-позиции с настоящим GPS ----------

def add_eval(ts: int, imei: str, gps_lat: float, gps_lon: float, loc: dict, err_m: float) -> None:
    with write_lock, db() as con:
        con.execute(
            "INSERT INTO wifi_eval(ts,imei,gps_lat,gps_lon,wifi_lat,wifi_lon,err_m,est_acc_m,used,scanned) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, imei, gps_lat, gps_lon, loc["lat"], loc["lon"], err_m,
             loc["accuracy_m"], loc["used"], loc["scanned"]),
        )


def eval_summary() -> dict:
    """Реальная точность: насколько Wi-Fi-позиция отличалась от GPS там, где GPS был хороший."""
    with db() as con:
        rows = con.execute("SELECT err_m, est_acc_m FROM wifi_eval").fetchall()
    n = len(rows)
    if not n:
        return {"n": 0}
    errs = sorted(r[0] for r in rows)

    def pct(p):
        return errs[min(n - 1, int(p * n))]

    return {
        "n": n,
        "median_m": round(pct(0.5), 1),
        "p90_m": round(pct(0.9), 1),
        "share_within_estimated_accuracy": round(sum(1 for e, a in rows if e <= a) / n, 3),
    }
