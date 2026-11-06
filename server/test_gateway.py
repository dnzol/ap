"""Тесты шлюза на живом TCP: поддельная Аксента принимает пакеты, поддельный трекер их шлёт.
Запуск:  python -m unittest test_gateway -v"""
import asyncio
import os
import tempfile
import time
import unittest

import apcore
import gateway as gw
from gateway import Cfg, Gateway, crc16, fmt_coord, parse_coord

IMEI = "123456789012345"
A = "aa:aa:aa:aa:aa:01"     # точка у (40.6400, 22.9440)
C = "cc:cc:cc:cc:cc:03"     # точка в ~470 м восточнее
T0 = 1_760_000_000


# ---------- помощники: пакеты Wialon IPS ----------

def dmsg(ts, lat, lon, nav, wifi=None, sats=9, hdop=1.0, speed=0, course=0, nav_name="valid_nav"):
    d, t = time.strftime("%d%m%y", time.gmtime(ts)), time.strftime("%H%M%S", time.gmtime(ts))
    la, lah = fmt_coord(lat, False)
    lo, loh = fmt_coord(lon, True)
    params = [f"{nav_name}:1:{nav}"] if nav is not None else []
    if wifi:
        params.append("wifi:3:" + " ".join(f"{b}={r}" for b, r in wifi))
    return ";".join([d, t, la, lah, lo, loh, str(speed), str(course), "NA", str(sats), str(hdop),
                     "NA", "NA", "NA", "NA", ",".join(params) or "NA"])


def pkt_d(msg, v2=True):
    if not v2:
        return f"#D#{msg}\r\n".encode()
    p = msg + ";"
    return f"#D#{p}{crc16(p.encode()):04X}\r\n".encode()


def pkt_b(msgs, v2=True):
    core = "|".join(msgs)
    if not v2:
        return f"#B#{core}\r\n".encode()
    p = core + ";"
    return f"#B#{p}{crc16(p.encode()):04X}\r\n".encode()


def login(v2=True):
    if not v2:
        return f"#L#{IMEI};pass\r\n".encode()
    p = f"2.0;{IMEI};pass;"
    return f"#L#{p}{crc16(p.encode()):04X}\r\n".encode()


def split_d(line: bytes, v2=True):
    """#D#...  -> (поля, список params, crc_ok)"""
    s = line.decode().rstrip("\r\n")
    assert s.startswith("#D#"), s
    body, ok = s[3:], True
    if v2:
        p, crc = body[:-4], body[-4:]
        ok = crc16(p.encode()) == int(crc, 16)
        body = p[:-1]
    f = body.split(";")
    assert len(f) == 16, f
    return f, f[15].split(","), ok


def pmap(params):
    out = {}
    for it in params:
        n, t, v = it.split(":", 2)
        out[n] = v
    return out


# ---------- поддельная Аксента ----------

class FakeAxenta:
    def __init__(self):
        self.received = []  # сырые строки без \r\n
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._h, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _h(self, r, w):
        try:
            while True:
                line = await r.readuntil(b"\n")
                s = line.rstrip(b"\r\n")
                self.received.append(s)
                if s.startswith(b"#L#"):
                    w.write(b"#AL#1\r\n")
                elif s.startswith(b"#D#"):
                    w.write(b"#AD#1\r\n")
                elif s.startswith(b"#B#"):
                    w.write(b"#AB#%d\r\n" % (s.count(b"|") + 1))
                elif s.startswith(b"#P#"):
                    w.write(b"#AP#\r\n")
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        apcore.DB_PATH = os.path.join(tempfile.mkdtemp(), "ap.db")
        apcore.init_db()
        self.ax = FakeAxenta()
        await self.ax.start()
        self._servers, self._writers = [], []

    async def asyncTearDown(self):
        for w in self._writers:
            w.close()
        for s in self._servers:
            s.close()
            await s.wait_closed()
        await self.ax.stop()

    async def start_gateway(self, **kw):
        cfg = Cfg(upstream_host="127.0.0.1", upstream_port=self.ax.port, **kw)
        g = Gateway(cfg)
        srv = await asyncio.start_server(g.handle, "127.0.0.1", 0, limit=2 ** 20)
        self._servers.append(srv)
        return g, srv.sockets[0].getsockname()[1]

    async def connect(self, port, v2=True):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        self._writers.append(w)
        w.write(login(v2))
        await w.drain()
        self.assertEqual(await asyncio.wait_for(r.readline(), 5), b"#AL#1\r\n")
        return r, w

    async def send(self, r, w, data: bytes, expect: bytes):
        w.write(data)
        await w.drain()
        got = await asyncio.wait_for(r.readline(), 5)
        self.assertEqual(got, expect)

    async def train(self, g, r, w, v2=True):
        """Тренировка: трекер с хорошим GPS видит A и C с трёх мест каждую (через 400 с, чтобы не сработал троттлинг)."""
        pts = []
        for i, (dla, dlo) in enumerate([(0, 0), (0.0002, 0.0002), (-0.0002, 0.0003)]):
            pts.append((T0 + i * 400, 40.6400 + dla, 22.9440 + dlo, [(A, -50 - 5 * i)]))
        for i, (dla, dlo) in enumerate([(0, 0), (0.0002, 0.0002), (-0.0002, 0.0003)]):
            pts.append((T0 + 1200 + i * 400, 40.6400 + dla, 22.9496 + dlo, [(C, -50 - 5 * i)]))
        for ts, la, lo, wf in pts:
            await self.send(r, w, pkt_d(dmsg(ts, la, lo, 1, wf), v2), b"#AD#1\r\n")
            await g.drain()  # в жизни пакеты идут с интервалом секунд; здесь ждём запись в базу явно
        return pts[-1][0]  # время последней GPS-точки (на востоке, рядом с C)

    # ---------- сценарии ----------

    async def test_substitution_speed_course_marks(self):
        g, port = await self.start_gateway(subst_all=True)
        r, w = await self.connect(port)
        t_last = await self.train(g, r, w)

        # учёба: тренировочные GPS-пакеты дошли до Аксенты как есть, база пополнилась
        self.assertGreaterEqual(g.stats["learned_obs"], 6)
        with apcore.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM obs WHERE source='tracker'").fetchone()[0],
                             g.stats["learned_obs"])
        self.assertGreaterEqual(apcore.eval_summary()["n"], 2)  # сравнение с GPS накапливается

        # трекер потерял GPS и видит только A (≈470 м западнее последней точки), через 60 с
        t1 = t_last + 60
        await self.send(r, w, pkt_d(dmsg(t1, 0, 0, 0, [(A, -50)], sats=0)), b"#AD#1\r\n")
        f, params, ok = split_d(self.ax.received[-1])
        self.assertTrue(ok, "CRC пересобранного пакета должен сходиться")
        lat = parse_coord(f[2], f[3])
        lon = parse_coord(f[4], f[5])
        self.assertAlmostEqual(lat, 40.6400, delta=0.0006)
        self.assertAlmostEqual(lon, 22.9441, delta=0.0006)
        pm = pmap(params)
        self.assertEqual(pm["nav_src"], "wifi")
        self.assertEqual(pm["wifi_n"], "1")
        self.assertGreaterEqual(float(pm["wifi_acc"]), 50)
        self.assertEqual(pm["valid_nav"], "0")                 # по умолчанию исходный флаг не трогаем
        self.assertIn("wifi", pm)                              # исходные параметры сохранены
        self.assertTrue(20 <= int(f[6]) <= 40, f"скорость {f[6]}")   # ~470 м за 60 с ≈ 28 км/ч
        self.assertTrue(255 <= int(f[7]) <= 285, f"курс {f[7]}")     # на запад
        first_pos = (f[2], f[3], f[4], f[5])

        # через 60 с та же картина: дрожание гасится, стоим на месте
        await self.send(r, w, pkt_d(dmsg(t1 + 60, 0, 0, 0, [(A, -53)], sats=0)), b"#AD#1\r\n")
        f2, _, ok2 = split_d(self.ax.received[-1])
        self.assertTrue(ok2)
        self.assertEqual((f2[2], f2[3], f2[4], f2[5]), first_pos)
        self.assertEqual(f2[6], "0")
        self.assertEqual(f2[7], f[7])                          # курс сохраняется

        # неизвестная сеть / нет Wi-Fi / valid_nav отсутствует: пакет уходит байт в байт
        for msg in (dmsg(t1 + 120, 1, 1, 0, [("de:ad:be:ef:00:01", -50)]),
                    dmsg(t1 + 121, 1, 1, 0, None),
                    dmsg(t1 + 122, 40.6, 22.9, None, [(A, -50)])):
            raw = pkt_d(msg)
            await self.send(r, w, raw, b"#AD#1\r\n")
            self.assertEqual(self.ax.received[-1] + b"\r\n", raw)

        self.assertEqual(g.stats["substituted"], 2)

    async def test_shadow_mode_does_not_modify(self):
        g, port = await self.start_gateway()        # SUBST_IMEIS не задан = наблюдение
        r, w = await self.connect(port)
        t_last = await self.train(g, r, w)
        raw = pkt_d(dmsg(t_last + 60, 0, 0, 0, [(A, -50)], sats=0))
        await self.send(r, w, raw, b"#AD#1\r\n")
        self.assertEqual(self.ax.received[-1] + b"\r\n", raw)
        self.assertEqual(g.stats["shadow"], 1)
        self.assertEqual(g.stats["substituted"], 0)
        self.assertGreaterEqual(g.stats["learned_obs"], 6)    # а учиться и сравнивать продолжает

    async def test_pilot_only_listed_imei(self):
        g, port = await self.start_gateway(subst_imeis=frozenset({"999"}))   # наш IMEI не в списке
        r, w = await self.connect(port)
        t_last = await self.train(g, r, w)
        raw = pkt_d(dmsg(t_last + 60, 0, 0, 0, [(A, -50)], sats=0))
        await self.send(r, w, raw, b"#AD#1\r\n")
        self.assertEqual(self.ax.received[-1] + b"\r\n", raw)

        g2, port2 = await self.start_gateway(subst_imeis=frozenset({IMEI}))  # теперь в списке
        r2, w2 = await self.connect(port2)
        await self.send(r2, w2, raw, b"#AD#1\r\n")
        self.assertNotEqual(self.ax.received[-1] + b"\r\n", raw)
        self.assertEqual(pmap(split_d(self.ax.received[-1])[1])["nav_src"], "wifi")

    async def test_bad_crc_forwarded_untouched(self):
        g, port = await self.start_gateway(subst_all=True)
        r, w = await self.connect(port)
        t_last = await self.train(g, r, w)
        good = pkt_d(dmsg(t_last + 60, 0, 0, 0, [(A, -50)], sats=0))
        bad = good[:-6] + b"0000\r\n"
        await self.send(r, w, bad, b"#AD#1\r\n")
        self.assertEqual(self.ax.received[-1] + b"\r\n", bad)
        self.assertEqual(g.stats["bad_crc"], 1)

    async def test_protocol_v11_without_crc(self):
        g, port = await self.start_gateway(subst_all=True, valid_nav_after="1")
        r, w = await self.connect(port, v2=False)
        t_last = await self.train(g, r, w, v2=False)
        await self.send(r, w, pkt_d(dmsg(t_last + 60, 0, 0, 0, [(A, -50)], sats=0), v2=False), b"#AD#1\r\n")
        f, params, _ = split_d(self.ax.received[-1], v2=False)
        pm = pmap(params)
        self.assertEqual(pm["nav_src"], "wifi")
        self.assertEqual(pm["valid_nav"], "1")                 # VALID_NAV_AFTER=1
        self.assertAlmostEqual(parse_coord(f[4], f[5]), 22.9441, delta=0.0006)

    async def test_blackbox_batch(self):
        g, port = await self.start_gateway(subst_all=True)
        r, w = await self.connect(port)
        t_last = await self.train(g, r, w)
        m1 = dmsg(t_last + 60, 0, 0, 0, [(A, -50)], sats=0)
        m2 = dmsg(t_last + 120, 0, 0, 0, [(A, -52)], sats=0)
        m3 = dmsg(t_last + 130, 40.6, 22.9, 1, None)            # обычная точка в середине пакета не меняется
        await self.send(r, w, pkt_b([m1, m2, m3]), b"#AB#3\r\n")
        s = self.ax.received[-1].decode()
        self.assertTrue(s.startswith("#B#"))
        body, crc = s[3:-4], s[-4:]
        self.assertEqual(crc16(body.encode()), int(crc, 16))
        msgs = body[:-1].split("|")
        self.assertEqual(len(msgs), 3)
        self.assertEqual(pmap(msgs[0].split(";")[15].split(","))["nav_src"], "wifi")
        self.assertEqual(pmap(msgs[1].split(";")[15].split(","))["nav_src"], "wifi")
        self.assertEqual(msgs[2], m3)

    async def test_other_packets_and_acks_pass_through(self):
        g, port = await self.start_gateway(subst_all=True)
        r, w = await self.connect(port)
        await self.send(r, w, b"#P#\r\n", b"#AP#\r\n")
        sd = b"#SD#041025;101010;4038.4000;N;02256.6400;E;0;0;NA;9\r\n"
        w.write(sd)
        await w.drain()
        await asyncio.sleep(0.2)
        self.assertEqual(self.ax.received[-1] + b"\r\n", sd)

    async def test_upstream_unavailable_closes_tracker(self):
        dead = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port_dead = dead.sockets[0].getsockname()[1]
        dead.close()
        await dead.wait_closed()
        g = Gateway(Cfg(upstream_host="127.0.0.1", upstream_port=port_dead))
        srv = await asyncio.start_server(g.handle, "127.0.0.1", 0)
        self._servers.append(srv)
        r, w = await asyncio.open_connection("127.0.0.1", srv.sockets[0].getsockname()[1])
        self._writers.append(w)
        self.assertEqual(await asyncio.wait_for(r.read(), 5), b"")   # соединение закрыто, трекер повторит сам
        self.assertEqual(g.stats["upstream_fail"], 1)

    async def test_outlier_jump_not_substituted(self):
        g, port = await self.start_gateway(subst_all=True)
        r, w = await self.connect(port)
        t_last = await self.train(g, r, w)
        # 470 м за 1 секунду = 1700 км/ч: считаем выбросом, пакет не трогаем
        raw = pkt_d(dmsg(t_last + 1, 0, 0, 0, [(A, -50)], sats=0))
        await self.send(r, w, raw, b"#AD#1\r\n")
        self.assertEqual(self.ax.received[-1] + b"\r\n", raw)
        self.assertEqual(g.stats["nav0_outlier"], 1)

    async def test_unit_helpers(self):
        self.assertAlmostEqual(parse_coord("4038.400000", "N"), 40.64)
        self.assertAlmostEqual(parse_coord("02256.640000", "W"), -22.944)
        self.assertEqual(fmt_coord(40.64, False), ("4038.400000", "N"))
        self.assertEqual(fmt_coord(-22.944, True), ("02256.640000", "W"))
        self.assertEqual(fmt_coord(10.9999999999, False)[0], "1100.000000")    # округление до 60 минут
        self.assertEqual(fmt_coord(10.99999999, False)[0], "1059.999999")
        import re
        rx = re.compile("^wifi", re.I)
        p = ["wifi1:3:AA-BB-CC-DD-EE-01 -61", "wifi2:3:aabbccddee02,55", "other:3:aa:bb:cc:dd:ee:03=-40"]
        self.assertEqual(gw.extract_wifi(p, rx), {"aa:bb:cc:dd:ee:01": -61, "aa:bb:cc:dd:ee:02": -55})
        self.assertEqual(crc16(b"123456789"), 0xBB3D)           # контрольное значение CRC-16/ARC


if __name__ == "__main__":
    unittest.main(verbosity=2)
