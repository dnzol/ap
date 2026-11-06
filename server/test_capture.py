"""Тест перехватчика: байты уходят в Аксенту без изменений, кадры NTCB попадают в лог.
Запуск:  python -m unittest test_capture -v"""
import asyncio
import unittest

import capture


def ntcb(data: bytes, idr=0, ids=1) -> bytes:
    h = b"@NTC" + idr.to_bytes(4, "little") + ids.to_bytes(4, "little") + len(data).to_bytes(2, "little")
    h += bytes([capture.xor(data)])
    h += bytes([capture.xor(h)])
    return h + data


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_unchanged_and_logged(self):
        got_by_upstream = bytearray()

        async def upstream(r, w):
            data = await r.readexactly(len(frame1) + len(frame2))
            got_by_upstream.extend(data)
            w.write(ntcb(b"*<S"))
            await w.drain()
            await asyncio.sleep(0.2)
            w.close()

        frame1 = ntcb(b"*>S:123456789012345")
        frame2 = ntcb(b"*>FLEX\xb0\x1e\x1e\x04\x0f")
        up = await asyncio.start_server(upstream, "127.0.0.1", 0)
        up_port = up.sockets[0].getsockname()[1]

        lines = []
        cap = capture.Capture("127.0.0.1", up_port, lines.append)
        srv = await asyncio.start_server(cap.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]

        r, w = await asyncio.open_connection("127.0.0.1", port)
        # кадры одним куском и вторым кадром, разорванным пополам: разбор потока должен это выдержать
        w.write(frame1 + frame2[:10])
        await w.drain()
        await asyncio.sleep(0.05)
        w.write(frame2[10:])
        await w.drain()
        reply = await asyncio.wait_for(r.read(1024), 5)
        w.close()
        await asyncio.sleep(0.3)
        up.close()
        srv.close()
        await up.wait_closed()
        await srv.wait_closed()

        self.assertEqual(bytes(got_by_upstream), frame1 + frame2)      # ничего не изменено
        self.assertEqual(reply, ntcb(b"*<S"))                          # ответ дошёл до терминала
        text = "\n".join(lines)
        self.assertIn("T→A NTCB", text)
        self.assertIn("A→T NTCB", text)
        self.assertIn("*>S:123456789012345", text)                     # ascii-предпросмотр рукопожатия
        self.assertIn("*>FLEX", text)
        self.assertEqual(text.count("csd=ok csp=ok"), 3)               # три кадра, суммы сошлись
        self.assertNotIn("bad", text)

    async def test_limit_and_garbage(self):
        lines = []
        cap = capture.Capture("127.0.0.1", 1, lines.append, max_frames=2)
        emit = cap._emit("peer")
        fl = capture.FrameLog(emit, "T→A")
        fl.feed(b"junk" + ntcb(b"*>S:1") + ntcb(b"*>S:2") + ntcb(b"*>S:3"))
        self.assertTrue(any("RAW" in x for x in lines))
        self.assertTrue(any("лимит" in x for x in lines))
        self.assertLessEqual(len(lines), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
