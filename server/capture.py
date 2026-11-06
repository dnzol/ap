#!/usr/bin/env python3
"""Прозрачный ретранслятор терминал → Аксента с записью трафика NTCB в hex. НИЧЕГО НЕ МЕНЯЕТ.

Нужен, чтобы увидеть реальные кадры терминала (рукопожатие, согласование набора полей FLEX, пакеты ~A/~T/~E)
и по ним писать разбор для шлюза. Байты пересылаются сразу и без изменений, запись идёт отдельно.

Кадр NTCB: заголовок 16 байт («@NTC», IDr u32, IDs u32, длина данных u16, XOR данных, XOR заголовка) + данные.
Контрольные суммы здесь только показываются (ok/bad), на пересылку они не влияют.

ВНИМАНИЕ: в логе есть IMEI терминала (кадр «*>S:...») и координаты. Перед отправкой лога кому-либо замажьте их."""
import asyncio
import logging
import os
import time
from typing import Callable

log = logging.getLogger("capture")

HEADER = 16
PREAMBLE = b"@NTC"


def xor(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


def printable(data: bytes, limit: int = 120) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data[:limit])


class FrameLog:
    """Разбирает поток одного направления на кадры NTCB и пишет их построчно (только для лога)."""

    def __init__(self, emit: Callable[[str], None], tag: str):
        self.buf = bytearray()
        self.emit = emit
        self.tag = tag

    def feed(self, data: bytes) -> None:
        self.buf += data
        while True:
            i = self.buf.find(PREAMBLE)
            if i < 0:
                if len(self.buf) > 3:  # хвост на случай, что преамбула разорвана между чтениями
                    self._raw(bytes(self.buf[:-3]))
                    del self.buf[:-3]
                return
            if i > 0:
                self._raw(bytes(self.buf[:i]))
                del self.buf[:i]
            if len(self.buf) < HEADER:
                return
            n = int.from_bytes(self.buf[12:14], "little")
            if len(self.buf) < HEADER + n:
                return
            frame = bytes(self.buf[:HEADER + n])
            del self.buf[:HEADER + n]
            self._frame(frame)

    def _raw(self, b: bytes) -> None:
        if b:
            self.emit(f"{self.tag} RAW len={len(b)} hex={b.hex()} ascii={printable(b)}")

    def _frame(self, f: bytes) -> None:
        idr = int.from_bytes(f[4:8], "little")
        ids = int.from_bytes(f[8:12], "little")
        n = int.from_bytes(f[12:14], "little")
        data = f[HEADER:]
        csd = "ok" if f[14] == xor(data) else "bad"
        csp = "ok" if f[15] == xor(f[:15]) else "bad"
        self.emit(f"{self.tag} NTCB idr={idr} ids={ids} len={n} csd={csd} csp={csp} "
                  f"hex={data.hex()} ascii={printable(data)}")


class Capture:
    def __init__(self, upstream_host: str, upstream_port: int, writer: Callable[[str], None],
                 max_frames: int = 400, idle_timeout_s: float = 600.0):
        self.host, self.port = upstream_host, upstream_port
        self.writer = writer
        self.max_frames = max_frames
        self.idle = idle_timeout_s
        self.frames = 0

    def _emit(self, peer: str):
        def emit(line: str) -> None:
            if self.frames >= self.max_frames:
                return
            self.frames += 1
            self.writer(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {peer} {line}")
            if self.frames == self.max_frames:
                self.writer(f"... достигнут лимит {self.max_frames} записей, дальше только пересылка")
        return emit

    async def handle(self, treader: asyncio.StreamReader, twriter: asyncio.StreamWriter) -> None:
        peer = str(twriter.get_extra_info("peername"))
        emit = self._emit(peer)
        uwriter = None
        try:
            try:
                ureader, uwriter = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 10)
            except Exception as e:
                log.warning("Аксента недоступна (%s:%s): %s", self.host, self.port, e)
                return
            emit("CONNECT")
            t1 = asyncio.create_task(self._pipe(treader, uwriter, FrameLog(emit, "T→A")))
            t2 = asyncio.create_task(self._pipe(ureader, twriter, FrameLog(emit, "A→T")))
            _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            emit("DISCONNECT")
        finally:
            for w in (twriter, uwriter):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    async def _pipe(self, reader, writer, flog: FrameLog) -> None:
        while True:
            try:
                data = await asyncio.wait_for(reader.read(65536), self.idle)
            except asyncio.TimeoutError:
                return
            if not data:
                return
            writer.write(data)          # сначала пересылка, лог не должен её задерживать или менять
            await writer.drain()
            try:
                flog.feed(data)
            except Exception:
                log.exception("ошибка записи лога (пересылка не затронута)")


async def serve() -> None:
    e = os.environ.get
    host, port = e("UPSTREAM_HOST", "").strip(), int(e("UPSTREAM_PORT", "0") or 0)
    if not host or not port:
        raise SystemExit("Задайте UPSTREAM_HOST и UPSTREAM_PORT (адрес и порт приёма терминалов в Аксенте)")
    path = e("CAPTURE_FILE", "/data/capture.log")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = open(path, "a", buffering=1, encoding="utf-8")

    def writer(line: str) -> None:
        fh.write(line + "\n")
        log.info(line[:200])

    cap = Capture(host, port, writer, max_frames=int(e("CAPTURE_MAX_FRAMES", "400")))
    server = await asyncio.start_server(cap.handle, e("LISTEN_HOST", "0.0.0.0"),
                                        int(e("LISTEN_PORT", "20332")), limit=2 ** 20)
    log.info("перехватчик слушает порт %s → Аксента %s:%s; лог: %s", e("LISTEN_PORT", "20332"), host, port, path)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve())
