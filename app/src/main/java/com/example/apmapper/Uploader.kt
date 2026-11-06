package com.example.apmapper

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/** Отправка ещё не отправленных наблюдений на сервер (POST {url}/upload, CSV в теле, Bearer-токен). */
object Uploader {

    class Result(
        val ok: Boolean,
        val message: String,
        val maxWifiId: Long = 0,
        val maxBleId: Long = 0
    )

    fun upload(db: Db, baseUrl: String, token: String, fromWifi: Long, fromBle: Long): Result {
        val (mw, mb) = db.maxIds()
        if (mw <= fromWifi && mb <= fromBle) return Result(false, "Новых данных нет")

        val url = try {
            URL(baseUrl.trim().trimEnd('/') + "/upload")
        } catch (e: Exception) {
            return Result(false, "Неверный адрес сервера")
        }
        // Токен нельзя гонять по сети открытым текстом.
        if (url.protocol != "https") return Result(false, "Нужен адрес с https://")

        val c = url.openConnection() as HttpURLConnection
        try {
            c.requestMethod = "POST"
            c.doOutput = true
            c.setChunkedStreamingMode(0) // стримим CSV, не собирая его целиком в памяти
            c.connectTimeout = 15_000
            c.readTimeout = 120_000
            c.setRequestProperty("Authorization", "Bearer ${token.trim()}")
            c.setRequestProperty("Content-Type", "text/csv; charset=utf-8")

            val exp = Exporter.writeWigle(db, c.outputStream, fromWifi, fromBle)

            val code = c.responseCode
            val body = (if (code in 200..299) c.inputStream else c.errorStream)
                ?.bufferedReader()?.use { it.readText() } ?: ""

            return when {
                code in 200..299 -> {
                    val j = runCatching { JSONObject(body) }.getOrNull()
                    val msg = if (j != null)
                        "Отправлено строк ${j.optInt("rows")}, новых ${j.optInt("new")}, " +
                            "отброшено сервером ${j.optInt("skipped")}"
                    else "Отправлено строк ${exp.wifiRows + exp.bleRows}"
                    Result(true, msg, exp.maxWifiId, exp.maxBleId)
                }
                code == 401 -> Result(false, "Сервер отклонил токен (401)")
                code == 413 -> Result(false, "Слишком большой файл для сервера (413)")
                else -> Result(false, "Ошибка сервера: HTTP $code")
            }
        } catch (e: Exception) {
            return Result(false, "Нет связи с сервером: ${e.message}")
        } finally {
            c.disconnect()
        }
    }
}
