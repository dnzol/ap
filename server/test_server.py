import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["API_TOKEN"] = "test-token"
os.environ["DB_PATH"] = os.path.join(_tmp, "ap.db")

from fastapi.testclient import TestClient  # noqa: E402

import apcore  # noqa: E402
import app as srv  # noqa: E402

H = {"Authorization": "Bearer test-token"}
HEADER = (
    "WigleWifi-1.4,appRelease=1.0,model=X,release=14,device=x,display=x,board=x,brand=x\n"
    "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
    "AltitudeMeters,AccuracyMeters,Type\n"
)


def line(mac, name, auth, t, ch, rssi, lat, lon, acc, typ):
    return f"{mac},{name},{auth},2026-10-06 {t},{ch},{rssi},{lat:.7f},{lon:.7f},30.0,{acc:.1f},{typ}\n"


def make_csv():
    out = HEADER
    # A: стационарная, наблюдается вокруг (40.6400, 22.9440), самое сильное — рядом с точкой
    a = "aa:aa:aa:aa:aa:01"
    for i, (dlat, dlon, rssi) in enumerate(
        [(0.0000, 0.0000, -45), (0.0004, 0.0000, -70), (-0.0004, 0.0000, -72),
         (0.0000, 0.0005, -75), (0.0000, -0.0005, -74)]
    ):
        out += line(a, '"Home, Net"', "[WPA2-PSK-CCMP][ESS]", f"10:00:{i:02d}", 6, rssi,
                    40.6400 + dlat, 22.9440 + dlon, 5.0, "WIFI")
    # B: стационарная, в ~700 м к востоку
    b = "bb:bb:bb:bb:bb:02"
    for i, (dlon, rssi) in enumerate([(0.0, -50), (0.0003, -65), (-0.0003, -66)]):
        out += line(b, "Cafe", "[ESS]", f"10:01:{i:02d}", 11, rssi, 40.6400, 22.9520 + dlon, 6.0, "WIFI")
    # C: подвижная — видна на расстоянии нескольких км
    c = "cc:cc:cc:cc:cc:03"
    for i, dlat in enumerate([0.0, 0.01, 0.02]):
        out += line(c, "Bus", "[ESS]", f"10:02:{i:02d}", 1, -60, 40.60 + dlat, 22.90, 5.0, "WIFI")
    # _nomap, плохие строки, не-маяк BLE, маяк BLE
    out += line("dd:dd:dd:dd:dd:04", "Neighbor_nomap", "[ESS]", "10:03:00", 1, -60, 40.6, 22.9, 5.0, "WIFI")
    out += line("not-a-mac", "X", "[ESS]", "10:03:01", 1, -60, 40.6, 22.9, 5.0, "WIFI")
    out += line("ee:ee:ee:ee:ee:05", "Bad acc", "[ESS]", "10:03:02", 1, -60, 40.6, 22.9, 500.0, "WIFI")
    out += line("11:22:33:44:55:66", "", "Misc", "10:03:03", 0, -70, 40.6, 22.9, 5.0, "BLE")
    for i, rssi in enumerate([-60, -62]):
        out += line("11:22:33:44:55:77", "ibeacon:abcd:1:2", "Misc", f"10:04:{i:02d}", 0, rssi,
                    40.6405, 22.9441, 5.0, "BLE")
    return out


def test_all():
    apcore.DB_PATH = os.path.join(tempfile.mkdtemp(), "ap.db")  # чистая база на каждый прогон
    with TestClient(srv.app) as cl:
        assert cl.get("/healthz").json() == {"ok": True}

        # авторизация
        assert cl.post("/upload", content=b"x").status_code == 401
        assert cl.post("/upload", content=b"x", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert cl.get("/stats").status_code == 401
        assert cl.post("/upload", content=b"", headers=H).status_code == 400

        body = make_csv().encode()
        r = cl.post("/upload", content=body, headers=H)
        assert r.status_code == 200, r.text
        j = r.json()
        # 5 + 3 + 3 валидных Wi-Fi + 2 маяка = 13 новых; отброшено 4 строки (nomap, плохой MAC, плохая точность, BLE-не-маяк)
        assert j["new"] == 13, j
        assert j["skipped"] == 4, j

        # повторная загрузка того же файла ничего не добавляет
        j2 = cl.post("/upload", content=body, headers=H).json()
        assert j2["new"] == 0, j2

        s = cl.get("/stats", headers=H).json()
        assert s["observations"] == 13
        assert s["wifi_aps"] == 3 and s["wifi_aps_static"] == 2 and s["ble_beacons"] == 1, s

        # подвижная точка не участвует в locate
        r = cl.post("/locate", json={"scan": [{"id": "CC:CC:CC:CC:CC:03", "rssi": -50}]}, headers=H)
        assert r.status_code == 404

        # одна точка A -> рядом с ней (центроид тянется к сильным наблюдениям)
        r = cl.post("/locate", json={"scan": [{"id": "AA:AA:AA:AA:AA:01", "rssi": -50}]}, headers=H)
        assert r.status_code == 200
        j = r.json()
        assert abs(j["lat"] - 40.6400) < 0.0003 and abs(j["lon"] - 22.9440) < 0.0003, j
        assert j["used"] == 1 and j["accuracy_m"] >= 50

        # A сильная + B слабая -> ближе к A, чем к B
        r = cl.post(
            "/locate",
            json={"scan": [{"id": "aa:aa:aa:aa:aa:01", "rssi": -45},
                           {"id": "bb:bb:bb:bb:bb:02", "rssi": -80},
                           {"id": "ff:00:00:00:00:99", "rssi": -40}]},
            headers=H,
        )
        assert r.status_code == 200
        j = r.json()
        assert j["used"] == 2 and j["scanned"] == 3
        assert abs(j["lon"] - 22.9440) < abs(j["lon"] - 22.9520), j

        # неизвестные сети
        r = cl.post("/locate", json={"scan": [{"id": "ff:00:00:00:00:99", "rssi": -40}]}, headers=H)
        assert r.status_code == 404

        # выгрузка базы точек
        txt = cl.get("/aps.csv", headers=H).text.strip().splitlines()
        assert txt[0].startswith("kind,key,ssid")
        assert len(txt) == 1 + 3  # A, B, маяк (C подвижная отфильтрована)
        assert any('"Home, Net"' in ln for ln in txt)


if __name__ == "__main__":
    test_all()
    print("OK")
