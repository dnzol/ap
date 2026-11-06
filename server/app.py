"""AP Mapper server: принимает CSV (WigleWifi-1.4) от приложения, хранит наблюдения,
считает позиции точек и умеет определять положение по списку видимых сетей.
Вся логика в apcore.py; здесь только HTTP."""
import csv
import io
import os
import secrets
from typing import List

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import apcore

API_TOKEN = os.environ.get("API_TOKEN", "")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024

app = FastAPI(title="AP Mapper server")
_process_upload = apcore.process_upload


@app.on_event("startup")
def _startup():
    if not API_TOKEN:
        raise RuntimeError("Задайте переменную окружения API_TOKEN")
    apcore.init_db()


def auth(request: Request):
    h = request.headers.get("authorization", "")
    token = h[7:] if h.lower().startswith("bearer ") else ""
    if not API_TOKEN or not secrets.compare_digest(token.encode(), API_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="bad token")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/upload", dependencies=[Depends(auth)])
async def upload(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="too large")
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    return await run_in_threadpool(_process_upload, bytes(body))


@app.get("/stats", dependencies=[Depends(auth)])
def stats():
    with apcore.db() as con:
        def q(sql):
            return con.execute(sql).fetchone()[0]
        return {
            "observations": q("SELECT COUNT(*) FROM obs"),
            "observations_from_trackers": q("SELECT COUNT(*) FROM obs WHERE source='tracker'"),
            "wifi_aps": q("SELECT COUNT(*) FROM aps WHERE kind='wifi'"),
            "wifi_aps_static": q("SELECT COUNT(*) FROM aps WHERE kind='wifi' AND is_static=1"),
            "ble_beacons": q("SELECT COUNT(*) FROM aps WHERE kind='ble'"),
            "uploads": q("SELECT COUNT(*) FROM uploads"),
        }


@app.get("/gateway/accuracy", dependencies=[Depends(auth)])
def gateway_accuracy():
    """Сравнение Wi-Fi-позиции с реальным GPS по данным трекеров (набирается, пока шлюз работает)."""
    return apcore.eval_summary()


@app.get("/aps.csv", dependencies=[Depends(auth)])
def aps_csv(only_static: bool = True):
    def gen():
        yield "kind,key,ssid,lat,lon,spread_m,n_obs,first_seen,last_seen,is_static\n"
        with apcore.db() as con:
            sql = "SELECT kind,key,ssid,lat,lon,spread_m,n_obs,first_seen,last_seen,is_static FROM aps"
            if only_static:
                sql += " WHERE is_static=1"
            for r in con.execute(sql):
                buf = io.StringIO()
                csv.writer(buf, lineterminator="\n").writerow(
                    [r[0], r[1], r[2], f"{r[3]:.7f}", f"{r[4]:.7f}", f"{r[5]:.1f}", r[6], r[7], r[8], r[9]]
                )
                yield buf.getvalue()
    return StreamingResponse(gen(), media_type="text/csv")


class ScanItem(BaseModel):
    id: str = Field(description="BSSID точки или id маяка")
    rssi: int = Field(ge=-127, le=0)


class LocateRequest(BaseModel):
    scan: List[ScanItem]


@app.post("/locate", dependencies=[Depends(auth)])
def locate(req: LocateRequest):
    scan = {}
    for it in req.scan:
        k = it.id.strip().lower()
        if k not in scan or it.rssi > scan[k]:
            scan[k] = it.rssi
    if not scan:
        raise HTTPException(status_code=400, detail="empty scan")
    if len(scan) > 200:
        raise HTTPException(status_code=400, detail="too many items")
    res = apcore.locate_scan(scan)
    if res is None:
        raise HTTPException(status_code=404, detail="no known access points in scan")
    return res
