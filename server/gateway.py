#!/usr/bin/env python3
"""Шлюз между трекерами и Аксентой (прозрачный TCP-прокси протокола Wialon IPS).

Трекер шлёт данные на шлюз вместо Аксенты, шлюз пересылает всё в Аксенту как есть, кроме двух случаев:

* valid_nav=0 и в пакете есть список Wi-Fi: координаты берутся из таблицы точек (apcore.locate_scan),
  подставляются в пакет, пакет помечается параметром nav_src:3:wifi (+ wifi_acc, wifi_n),
  скорость и курс считаются по предыдущей точке этого трекера;
* valid_nav=1 и в пакете есть список Wi-Fi: пары «сети + хороший GPS» пополняют таблицу точек,
  а Wi-Fi-позиция сравнивается с настоящим GPS (таблица wifi_eval, /gateway/accuracy).

Любая ошибка разбора = пакет уходит в Аксенту без изменений. Подмена по умолчанию выключена (режим
наблюдения): включается списком IMEI (SUBST_IMEIS=imei1,imei2) или для всех (SUBST_IMEIS=*).

ДОПУЩЕНИЯ, которые нужно сверить с реальным трекером и Аксентой (см. README): протокол Wialon IPS 1.1/2.0,
valid_nav лежит в поле params, список Wi-Fi лежит в параметрах с именем wifi*, CRC16 для IPS 2.0 считается
как CRC-16/ARC по данным пакета вместе с последней ';'."""
import asyncio
import logging
import math
import os
import re
import time
from calendar import timegm
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import apcore

log = logging.getLogger("gateway")


# ---------- конфигурация ----------

@dataclass
class Cfg:
    listen_host: str = "0.0.0.0"
    listen_port: int = 20332
    upstream_host: str = ""
    upstream_port: int = 0
    valid_nav_param: str = "valid_nav"
    wifi_param_re: str = r"^wifi"
    mark_param: str = "nav_src"
    subst_all: bool = False
    subst_imeis: frozenset = frozenset()  # пусто и subst_all=False: режим наблюдения, пакеты не меняются
    valid_nav_after: str = "keep"  # keep: оставить 0 как есть; "1": после подмены ставить 1
    sats_override: Optional[int] = None  # если Аксента считает точку годной только при ненулевых спутниках
    learn: bool = True
    learn_min_sats: int = 5
    learn_max_hdop: float = 2.5
    learn_max_speed_kmh: float = 40.0
    learn_min_interval_s: int = 300  # одну и ту же точку с того же места чаще не пишем
    learn_min_move_m: float = 15.0
    eval_interval_s: int = 60
    stationary_m: float = 30.0  # смещение меньше этого (и меньше оценки точности) = стоим на месте
    stationary_cap_m: float = 150.0
    max_speed_kmh: float = 150.0  # скачок быстрее считаем выбросом и пакет не трогаем
    idle_timeout_s: float = 600.0
    connect_timeout_s: float = 10.0
    max_conns: int = 500

    @classmethod
    def from_env(cls) -> "Cfg":
        e = os.environ.get
        imeis = e("SUBST_IMEIS", "").strip()
        sats = e("SATS_OVERRIDE", "").strip()
        return cls(
            listen_host=e("LISTEN_HOST", "0.0.0.0"),
            listen_port=int(e("LISTEN_PORT", "20332")),
            upstream_host=e("UPSTREAM_HOST", "").strip(),
            upstream_port=int(e("UPSTREAM_PORT", "0") or 0),
            valid_nav_param=e("VALID_NAV_PARAM", "valid_nav"),
            wifi_param_re=e("WIFI_PARAM_RE", r"^wifi"),
            mark_param=e("MARK_PARAM", "nav_src"),
            subst_all=(imeis == "*"),
            subst_imeis=frozenset(x.strip() for x in imeis.split(",") if x.strip() and x.strip() != "*"),
            valid_nav_after=e("VALID_NAV_AFTER", "keep"),
            sats_override=int(sats) if sats else None,
            learn=e("LEARN", "1") != "0",
            learn_min_sats=int(e("LEARN_MIN_SATS", "5")),
            learn_max_hdop=float(e("LEARN_MAX_HDOP", "2.5")),
            learn_max_speed_kmh=float(e("LEARN_MAX_SPEED_KMH", "40")),
            stationary_m=float(e("STATIONARY_M", "30")),
            max_speed_kmh=float(e("MAX_SPEED_KMH", "150")),
            idle_timeout_s=float(e("IDLE_TIMEOUT_S", "600")),
            max_conns=int(e("MAX_CONNS", "500")),
        )


# ---------- Wialon IPS: CRC, координаты, время, параметры ----------

def crc16(data: bytes) -> int:
    """CRC-16/ARC (полином 0xA001, начальное значение 0), как в Wialon IPS 2.0."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


_CRC_TAIL = re.compile(r";([0-9A-Fa-f]{4})$")
_MAC_RE = re.compile(r"(?<![0-9A-Fa-f])((?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}|[0-9A-Fa-f]{12})(?![0-9A-Fa-f])")
_NEG = re.compile(r"-\d{1,3}")
_POS = re.compile(r"\d{1,3}")

# поля пакета #D#: дата;время;lat1;lat2;lon1;lon2;скорость;курс;высота;спутники;hdop;входы;выходы;adc;ibutton;params
N_FIELDS = 16
F_DATE, F_TIME, F_LAT, F_LATH, F_LON, F_LONH, F_SPEED, F_COURSE, F_ALT, F_SATS, F_HDOP, F_PARAMS = (
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15)


def fnum(s) -> Optional[float]:
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_coord(val: str, hemi: str) -> Optional[float]:
    """'4038.4000' + 'N' -> 40.64 (градусы и минуты -> десятичные градусы)."""
    x = fnum(val)
    if x is None or hemi not in ("N", "S", "E", "W"):
        return None
    deg = int(x // 100)
    v = deg + (x - deg * 100) / 60.0
    return -v if hemi in ("S", "W") else v


def fmt_coord(v: float, is_lon: bool) -> Tuple[str, str]:
    hemi = ("E" if v >= 0 else "W") if is_lon else ("N" if v >= 0 else "S")
    a = abs(v)
    deg = int(a)
    minutes = round((a - deg) * 60.0, 6)
    if minutes >= 60.0:
        deg += 1
        minutes = 0.0
    return (f"{deg:03d}" if is_lon else f"{deg:02d}") + f"{minutes:09.6f}", hemi


def parse_ts(d: str, t: str) -> Optional[int]:
    try:
        return timegm(time.strptime(d + t, "%d%m%y%H%M%S"))
    except ValueError:
        return None


def pget(items: List[str], name: str) -> Optional[Tuple[str, str]]:
    """Параметр 'имя:тип:значение' -> (тип, значение)."""
    name = name.lower()
    for it in items:
        p = it.split(":", 2)
        if len(p) == 3 and p[0].lower() == name:
            return p[1], p[2]
    return None


def pset(items: List[str], name: str, typ: str, val: str) -> None:
    name_l = name.lower()
    for i, it in enumerate(items):
        if it.split(":", 2)[0].lower() == name_l:
            items[i] = f"{name}:{typ}:{val}"
            return
    items.append(f"{name}:{typ}:{val}")


def extract_wifi(items: List[str], name_re) -> Dict[str, int]:
    """Достаёт {bssid: rssi} из параметров, чьё имя подходит под name_re. Формат терпимый: MAC с ':', '-' или без
    разделителей, RSSI следом за ним (отрицательный предпочтительнее; положительный трактуем как модуль)."""
    scan: Dict[str, int] = {}
    for it in items:
        p = it.split(":", 2)
        if len(p) != 3 or not name_re.search(p[0]):
            continue
        val = p[2]
        ms = list(_MAC_RE.finditer(val))
        for i, m in enumerate(ms):
            bssid = apcore.normalize_bssid(m.group(1))
            if not bssid:
                continue
            tail = val[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(val)]
            r = _NEG.search(tail) or _POS.search(tail)
            rssi = int(r.group()) if r else -75
            if rssi > 0:
                rssi = -rssi
            if not -127 <= rssi <= -1:
                rssi = -75
            if bssid not in scan or rssi > scan[bssid]:
                scan[bssid] = rssi
    return scan


# ---------- состояние ----------

@dataclass
class Last:
    ts: int
    lat: float
    lon: float
    course: float


@dataclass
class Conn:
    peer: str
    imei: str = ""
    v2: bool = False
    last: Optional[Last] = None
    seen: Dict[str, Tuple[int, float, float]] = field(default_factory=dict)
    eval_ts: int = 0


class Gateway:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        self.stats: Counter = Counter()
        self.conns = 0
        self.wifi_re = re.compile(cfg.wifi_param_re, re.I)
        self._bg: set = set()

    # ----- фоновые задачи (запись в БД не задерживает пересылку) -----

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def drain(self) -> None:
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    # ----- разбор пакета -----

    def _login(self, rest: str, conn: Conn) -> None:
        parts = rest.split(";")
        if parts and parts[0] == "2.0":
            conn.v2 = True
            conn.imei = parts[1] if len(parts) > 1 else ""
        else:
            conn.imei = parts[0] if parts else ""
        self.stats["logins"] += 1

    async def process_line(self, line: bytes, conn: Conn) -> bytes:
        """line без \\r\\n -> что отправить в Аксенту (тоже без \\r\\n). При любой ошибке возвращает line как есть."""
        try:
            s = line.decode("latin-1")
            if not s.startswith("#"):
                return line
            i = s.find("#", 1)
            if i < 0:
                return line
            kind, rest = s[1:i], s[i + 1:]
            if kind == "L":
                self._login(rest, conn)
                return line
            if kind not in ("D", "B"):
                return line
            self.stats["data_packets"] += 1

            has_crc = False
            payload = rest
            m = _CRC_TAIL.search(rest)
            if m:
                cand = rest[: m.start() + 1]  # данные вместе с последней ';'
                if crc16(cand.encode("latin-1")) == int(m.group(1), 16):
                    has_crc, payload = True, cand
                elif conn.v2:
                    self.stats["bad_crc"] += 1
                    return line
            elif conn.v2:
                self.stats["no_crc"] += 1
                return line

            core = payload[:-1] if has_crc else payload
            msgs = core.split("|") if kind == "B" else [core]
            out, changed = [], False
            for msg in msgs:
                nf = await self.process_message(msg.split(";"), conn)
                if nf is None:
                    out.append(msg)
                else:
                    out.append(";".join(nf))
                    changed = True
            if not changed:
                return line
            new_core = "|".join(out)
            if has_crc:
                payload2 = new_core + ";"
                return f"#{kind}#{payload2}{crc16(payload2.encode('latin-1')):04X}".encode("latin-1")
            return f"#{kind}#{new_core}".encode("latin-1")
        except Exception:
            self.stats["errors"] += 1
            log.exception("ошибка обработки пакета, пересылаю без изменений (imei=%s)", conn.imei)
            return line

    async def process_message(self, f: List[str], conn: Conn) -> Optional[List[str]]:
        """Одно сообщение с данными. Возвращает новые поля, если пакет нужно изменить, иначе None."""
        cfg = self.cfg
        if len(f) != N_FIELDS:
            return None
        params = [] if f[F_PARAMS] in ("", "NA") else f[F_PARAMS].split(",")
        nav_p = pget(params, cfg.valid_nav_param)
        nav_v = fnum(nav_p[1]) if nav_p else None
        if nav_v is None or int(nav_v) not in (0, 1):
            return None
        nav = int(nav_v)
        ts = parse_ts(f[F_DATE], f[F_TIME])
        if ts is None:
            return None
        lat = parse_coord(f[F_LAT], f[F_LATH])
        lon = parse_coord(f[F_LON], f[F_LONH])
        scan = extract_wifi(params, self.wifi_re)

        if nav == 1:
            self._on_gps(f, conn, ts, lat, lon, scan)
            return None
        return await self._on_nav0(f, params, nav_p, conn, ts, scan)

    # ----- valid_nav=1: учимся и сравниваем -----

    def _on_gps(self, f, conn: Conn, ts: int, lat, lon, scan: Dict[str, int]) -> None:
        cfg = self.cfg
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            return
        course = fnum(f[F_COURSE])
        conn.last = Last(ts, lat, lon, course if course is not None else (conn.last.course if conn.last else 0.0))
        self.stats["gps_points"] += 1
        if not cfg.learn or not scan:
            return
        sats, hdop, speed = fnum(f[F_SATS]), fnum(f[F_HDOP]), fnum(f[F_SPEED])
        if sats is None or sats < cfg.learn_min_sats:
            self.stats["learn_skip_gps_quality"] += 1
            return
        if hdop is not None and hdop > cfg.learn_max_hdop:
            self.stats["learn_skip_gps_quality"] += 1
            return
        if speed is not None and speed > cfg.learn_max_speed_kmh:
            self.stats["learn_skip_speed"] += 1
            return
        acc = max(5.0, hdop * 5.0) if hdop else 10.0

        fresh = {}
        for b, r in scan.items():
            s = conn.seen.get(b)
            if s and ts - s[0] < cfg.learn_min_interval_s and apcore.dist_m(s[1], s[2], lat, lon) < cfg.learn_min_move_m:
                continue
            fresh[b] = r
            conn.seen[b] = (ts, lat, lon)
        if len(conn.seen) > 2000:
            conn.seen.clear()
        do_eval = ts - conn.eval_ts >= cfg.eval_interval_s
        if do_eval:
            conn.eval_ts = ts
        if fresh or do_eval:
            self._spawn(self._learn(conn.imei, ts, lat, lon, acc, dict(scan), fresh, do_eval))

    async def _learn(self, imei, ts, lat, lon, acc, scan, fresh, do_eval) -> None:
        try:
            if do_eval:  # сначала оценка по текущей таблице, потом пополнение: иначе сравнивали бы сами с собой
                loc = await asyncio.to_thread(apcore.locate_scan, scan)
                if loc:
                    err = apcore.dist_m(lat, lon, loc["lat"], loc["lon"])
                    await asyncio.to_thread(apcore.add_eval, ts, imei, lat, lon, loc, err)
                    self.stats["eval_rows"] += 1
            if fresh:
                rows = [("wifi", b, "", "", 0, r, lat, lon, 0.0, acc, ts) for b, r in fresh.items()]
                n = await asyncio.to_thread(apcore.add_tracker_observations, rows)
                self.stats["learned_obs"] += n
        except Exception:
            self.stats["errors"] += 1
            log.exception("не удалось записать наблюдения (imei=%s)", imei)

    # ----- valid_nav=0: подмена -----

    async def _on_nav0(self, f, params, nav_p, conn: Conn, ts: int, scan: Dict[str, int]) -> Optional[List[str]]:
        cfg = self.cfg
        self.stats["nav0_points"] += 1
        if not scan:
            self.stats["nav0_no_wifi"] += 1
            return None
        loc = await asyncio.to_thread(apcore.locate_scan, scan)
        if not loc:
            self.stats["nav0_no_match"] += 1
            return None
        lat, lon, acc = loc["lat"], loc["lon"], loc["accuracy_m"]

        speed, course = 0.0, 0.0
        prev = conn.last
        if prev:
            d = apcore.dist_m(prev.lat, prev.lon, lat, lon)
            dt = ts - prev.ts
            thr = min(cfg.stationary_cap_m, max(cfg.stationary_m, acc))
            if d < thr:  # дрожание Wi-Fi-оценки: стоим на месте, координата не «гуляет»
                lat, lon, course = prev.lat, prev.lon, prev.course
            else:
                if dt > 0:
                    v = d / dt * 3.6
                    if v > cfg.max_speed_kmh:
                        self.stats["nav0_outlier"] += 1
                        return None
                    speed = v
                course = apcore.bearing_deg(prev.lat, prev.lon, lat, lon)
        conn.last = Last(ts, lat, lon, course)

        if not (cfg.subst_all or conn.imei in cfg.subst_imeis):
            self.stats["shadow"] += 1
            log.info("[наблюдение] imei=%s подставил бы %.6f,%.6f ±%.0f м (сетей %d), скорость %.0f, курс %.0f",
                     conn.imei, lat, lon, acc, loc["used"], speed, course)
            return None

        nf = list(f)
        nf[F_LAT], nf[F_LATH] = fmt_coord(lat, False)
        nf[F_LON], nf[F_LONH] = fmt_coord(lon, True)
        nf[F_SPEED] = str(int(round(speed)))
        nf[F_COURSE] = str(int(round(course)))
        if cfg.sats_override is not None:
            nf[F_SATS] = str(cfg.sats_override)
        pset(params, cfg.mark_param, "3", "wifi")
        pset(params, "wifi_acc", "2", f"{acc:.0f}")
        pset(params, "wifi_n", "1", str(loc["used"]))
        if cfg.valid_nav_after == "1":
            pset(params, cfg.valid_nav_param, nav_p[0], "1")
        nf[F_PARAMS] = ",".join(params)
        self.stats["substituted"] += 1
        return nf

    # ----- сеть -----

    async def handle(self, treader: asyncio.StreamReader, twriter: asyncio.StreamWriter) -> None:
        peer = str(twriter.get_extra_info("peername"))
        if self.conns >= self.cfg.max_conns:
            self.stats["rejected_max_conns"] += 1
            twriter.close()
            return
        self.conns += 1
        self.stats["connections"] += 1
        conn = Conn(peer)
        uwriter = None
        try:
            try:
                ureader, uwriter = await asyncio.wait_for(
                    asyncio.open_connection(self.cfg.upstream_host, self.cfg.upstream_port),
                    self.cfg.connect_timeout_s)
            except Exception as e:  # трекер получит обрыв и сам повторит позже, данные у него сохранятся
                self.stats["upstream_fail"] += 1
                log.warning("Аксента недоступна (%s:%s): %s", self.cfg.upstream_host, self.cfg.upstream_port, e)
                return
            t1 = asyncio.create_task(self._tracker_to_upstream(treader, uwriter, conn))
            t2 = asyncio.create_task(self._upstream_to_tracker(ureader, twriter))
            _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self.conns -= 1
            for w in (twriter, uwriter):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    async def _tracker_to_upstream(self, treader, uwriter, conn: Conn) -> None:
        while True:
            try:
                line = await asyncio.wait_for(treader.readuntil(b"\n"), self.cfg.idle_timeout_s)
            except asyncio.IncompleteReadError as e:
                if e.partial:
                    uwriter.write(e.partial)
                    await uwriter.drain()
                return
            except asyncio.LimitOverrunError as e:  # слишком длинная «строка»: пересылаем кусок как есть
                chunk = await treader.readexactly(e.consumed)
                uwriter.write(chunk)
                await uwriter.drain()
                continue
            except asyncio.TimeoutError:
                return
            body = line[:-2] if line.endswith(b"\r\n") else line[:-1]
            eol = line[len(body):]
            out = await self.process_line(body, conn)
            uwriter.write(out + eol)
            await uwriter.drain()

    async def _upstream_to_tracker(self, ureader, twriter) -> None:
        while True:
            data = await ureader.read(65536)
            if not data:
                return
            twriter.write(data)
            await twriter.drain()


async def serve(cfg: Cfg) -> None:
    if not cfg.upstream_host or not cfg.upstream_port:
        raise SystemExit("Задайте UPSTREAM_HOST и UPSTREAM_PORT (адрес и порт приёма трекеров в Аксенте)")
    apcore.init_db()
    gw = Gateway(cfg)
    server = await asyncio.start_server(gw.handle, cfg.listen_host, cfg.listen_port, limit=2 ** 20)
    mode = ("подмена для ВСЕХ" if cfg.subst_all else
            f"подмена для IMEI: {sorted(cfg.subst_imeis)}" if cfg.subst_imeis else "режим наблюдения (пакеты не меняются)")
    log.info("шлюз слушает %s:%s → Аксента %s:%s; %s; пополнение базы: %s",
             cfg.listen_host, cfg.listen_port, cfg.upstream_host, cfg.upstream_port, mode,
             "да" if cfg.learn else "нет")

    async def report():
        while True:
            await asyncio.sleep(300)
            log.info("счётчики: %s; соединений сейчас: %d", dict(gw.stats), gw.conns)

    asyncio.create_task(report())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve(Cfg.from_env()))
