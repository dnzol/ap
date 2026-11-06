package com.example.apmapper

import android.os.Build
import java.io.OutputStream
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone

/** Сколько строк записано и какие наибольшие id попали в файл (для инкрементальной отправки). */
class ExportResult(val wifiRows: Int, val bleRows: Int, val maxWifiId: Long, val maxBleId: Long)

/** Экспорт в CSV формата WiGLE (WigleWifi-1.4): его понимают WiGLE, Kismet-конвертеры и свои скрипты. */
object Exporter {

    /** Пишет наблюдения с id больше заданных (0 = всё) и закрывает [out]. */
    fun writeWigle(db: Db, out: OutputStream, fromWifiId: Long = 0, fromBleId: Long = 0): ExportResult {
        val fmt = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("UTC")
        }
        var maxW = fromWifiId
        var maxB = fromBleId
        var nW = 0
        var nB = 0

        out.bufferedWriter(Charsets.UTF_8).use { w ->
            w.write(
                "WigleWifi-1.4,appRelease=1.0,model=${clean(Build.MODEL)},release=${clean(Build.VERSION.RELEASE)}," +
                    "device=${clean(Build.DEVICE)},display=${clean(Build.DISPLAY)},board=${clean(Build.BOARD)}," +
                    "brand=${clean(Build.BRAND)}\n"
            )
            w.write("MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n")

            val rdb = db.readableDatabase

            rdb.rawQuery(
                "SELECT bssid, ssid, caps, freq, rssi, lat, lon, alt, acc, ts, id " +
                    "FROM wifi_obs WHERE id > ? ORDER BY id",
                arrayOf(fromWifiId.toString())
            ).use { c ->
                while (c.moveToNext()) {
                    w.write(
                        row(
                            c.getString(0), c.getString(1) ?: "", c.getString(2) ?: "",
                            fmt.format(c.getLong(9)), channel(c.getInt(3)), c.getInt(4),
                            c.getDouble(5), c.getDouble(6), c.getDouble(7), c.getDouble(8), "WIFI"
                        )
                    )
                    maxW = c.getLong(10)
                    nW++
                }
            }

            rdb.rawQuery(
                "SELECT mac, beacon_id, rssi, lat, lon, alt, acc, ts, id " +
                    "FROM ble_obs WHERE id > ? ORDER BY id",
                arrayOf(fromBleId.toString())
            ).use { c ->
                while (c.moveToNext()) {
                    // Для маяков в колонку SSID кладём их стабильный id (uuid:major:minor).
                    w.write(
                        row(
                            c.getString(0), c.getString(1) ?: "", "Misc",
                            fmt.format(c.getLong(7)), 0, c.getInt(2),
                            c.getDouble(3), c.getDouble(4), c.getDouble(5), c.getDouble(6), "BLE"
                        )
                    )
                    maxB = c.getLong(8)
                    nB++
                }
            }
        }
        return ExportResult(nW, nB, maxW, maxB)
    }

    private fun row(
        mac: String, name: String, auth: String, time: String, ch: Int, rssi: Int,
        lat: Double, lon: Double, alt: Double, acc: Double, type: String
    ): String =
        listOf(
            mac, esc(name), esc(auth), time, ch.toString(), rssi.toString(),
            String.format(Locale.US, "%.7f", lat), String.format(Locale.US, "%.7f", lon),
            String.format(Locale.US, "%.1f", alt), String.format(Locale.US, "%.1f", acc), type
        ).joinToString(",") + "\n"

    private fun esc(s: String): String =
        if (s.any { it == ',' || it == '"' || it == '\n' || it == '\r' })
            "\"" + s.replace("\"", "\"\"").replace("\n", " ").replace("\r", " ") + "\""
        else s

    private fun clean(s: String?): String = (s ?: "").replace(",", " ")

    private fun channel(freq: Int): Int = when {
        freq == 2484 -> 14
        freq in 2412..2472 -> (freq - 2407) / 5
        freq in 5160..5885 -> (freq - 5000) / 5
        freq in 5955..7115 -> (freq - 5950) / 5
        else -> 0
    }
}
