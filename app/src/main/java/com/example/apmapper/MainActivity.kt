package com.example.apmapper

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

class MainActivity : Activity() {

    private val reqPerms = 1
    private val reqExport = 2

    private lateinit var prefs: SharedPreferences
    private lateinit var tvStatus: TextView
    private lateinit var tvStats: TextView
    private lateinit var btnToggle: Button
    private lateinit var etAcc: EditText
    private lateinit var cbWifi: CheckBox
    private lateinit var cbBle: CheckBox
    private lateinit var cbBeacons: CheckBox
    private lateinit var etUrl: EditText
    private lateinit var etToken: EditText
    private lateinit var tvUpload: TextView
    private lateinit var btnUpload: Button

    private val ui = Handler(Looper.getMainLooper())
    private val bg = Executors.newSingleThreadExecutor()
    private val net = Executors.newSingleThreadExecutor()
    private val tick = object : Runnable {
        override fun run() {
            refreshUi()
            ui.postDelayed(this, 2000)
        }
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences("prefs", MODE_PRIVATE)

        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(16), dp(16), dp(24))
        }
        val scroll = ScrollView(this).apply {
            addView(col)
            fitsSystemWindows = true
        }
        setContentView(scroll)

        col.addView(TextView(this).apply {
            text = "AP Mapper"
            textSize = 24f
        })

        tvStatus = TextView(this).apply {
            textSize = 16f
            setPadding(0, dp(12), 0, dp(4))
        }
        col.addView(tvStatus)

        tvStats = TextView(this).apply {
            textSize = 14f
            setPadding(0, 0, 0, dp(12))
        }
        col.addView(tvStats)

        btnToggle = Button(this).apply {
            setOnClickListener { onToggle() }
        }
        col.addView(btnToggle)

        col.addView(TextView(this).apply {
            text = "Что записывать"
            textSize = 16f
            setPadding(0, dp(20), 0, dp(4))
        })

        cbWifi = CheckBox(this).apply {
            text = "Wi‑Fi точки доступа"
            isChecked = prefs.getBoolean("wifi", true)
        }
        col.addView(cbWifi)

        cbBle = CheckBox(this).apply {
            text = "Bluetooth LE"
            isChecked = prefs.getBoolean("ble", true)
        }
        col.addView(cbBle)

        cbBeacons = CheckBox(this).apply {
            text = "BLE: только маяки (iBeacon / Eddystone)"
            isChecked = prefs.getBoolean("beacons_only", true)
        }
        col.addView(cbBeacons)

        col.addView(TextView(this).apply {
            text = "Если снять галочку, будут записываться и обычные BLE-устройства рядом " +
                "(кроме случайных адресов). Это могут быть чужие телефоны, часы, наушники — " +
                "для геолокации они бесполезны, а хранить такие данные о людях не стоит."
            textSize = 12f
        })

        col.addView(TextView(this).apply {
            text = "Макс. погрешность GPS, м (хуже — не пишем)"
            setPadding(0, dp(16), 0, 0)
        })
        etAcc = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setText(prefs.getInt("min_acc", 30).toString())
        }
        col.addView(etAcc)

        col.addView(TextView(this).apply {
            text = "Данные"
            textSize = 16f
            setPadding(0, dp(20), 0, dp(4))
        })

        col.addView(Button(this).apply {
            text = "Экспорт в CSV (формат WiGLE)"
            setOnClickListener { onExport() }
        })
        col.addView(Button(this).apply {
            text = "Очистить базу"
            setOnClickListener { onClear() }
        })

        col.addView(TextView(this).apply {
            text = "Сервер"
            textSize = 16f
            setPadding(0, dp(20), 0, dp(4))
        })
        etUrl = EditText(this).apply {
            hint = "https://example.com"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setText(prefs.getString("server_url", "") ?: "")
        }
        col.addView(etUrl)
        etToken = EditText(this).apply {
            hint = "Токен"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(prefs.getString("server_token", "") ?: "")
        }
        col.addView(etToken)
        btnUpload = Button(this).apply {
            text = "Отправить на сервер"
            setOnClickListener { onUpload() }
        }
        col.addView(btnUpload)
        tvUpload = TextView(this).apply {
            textSize = 12f
            text = prefs.getString("last_upload", "") ?: ""
        }
        col.addView(tvUpload)

        col.addView(TextView(this).apply {
            text = "Для записи нужен включённый GPS (геолокация). Лучше выходить на открытое место и " +
                "дождаться точности GPS. На Android 9+ система ограничивает частоту Wi‑Fi-сканов " +
                "(≈4 за 2 минуты); ограничение отключается в «Параметрах разработчика» → " +
                "«Регулировка поиска Wi‑Fi»."
            textSize = 12f
            setPadding(0, dp(20), 0, 0)
        })
    }

    override fun onResume() {
        super.onResume()
        ui.post(tick)
    }

    override fun onPause() {
        super.onPause()
        ui.removeCallbacks(tick)
        savePrefs()
    }

    // ---------- управление ----------

    private fun savePrefs() {
        val acc = etAcc.text.toString().toIntOrNull()?.coerceIn(3, 500) ?: 30
        prefs.edit()
            .putBoolean("wifi", cbWifi.isChecked)
            .putBoolean("ble", cbBle.isChecked)
            .putBoolean("beacons_only", cbBeacons.isChecked)
            .putInt("min_acc", acc)
            .putString("server_url", etUrl.text.toString().trim())
            .putString("server_token", etToken.text.toString().trim())
            .apply()
    }

    private fun neededPerms(): List<String> {
        val l = mutableListOf(Manifest.permission.ACCESS_FINE_LOCATION)
        if (Build.VERSION.SDK_INT >= 31) l += Manifest.permission.BLUETOOTH_SCAN
        if (Build.VERSION.SDK_INT >= 33) l += Manifest.permission.POST_NOTIFICATIONS
        return l
    }

    private fun missingPerms() =
        neededPerms().filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }

    private fun onToggle() {
        if (ScanState.running) {
            stopService(Intent(this, ScanService::class.java))
            ScanState.running = false
            refreshUi()
            return
        }
        savePrefs()
        if (!cbWifi.isChecked && !cbBle.isChecked) {
            Toast.makeText(this, "Выберите, что записывать", Toast.LENGTH_SHORT).show()
            return
        }
        val miss = missingPerms()
        if (miss.isNotEmpty()) {
            requestPermissions(miss.toTypedArray(), reqPerms)
            return
        }
        startScanning()
    }

    private fun startScanning() {
        val lm = getSystemService(LOCATION_SERVICE) as LocationManager
        if (!lm.isProviderEnabled(LocationManager.GPS_PROVIDER)) {
            Toast.makeText(this, "Включите геолокацию (GPS) в настройках", Toast.LENGTH_LONG).show()
            return
        }
        startForegroundService(Intent(this, ScanService::class.java))
        ScanState.running = true
        refreshUi()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != reqPerms) return
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) {
            startScanning()
        } else {
            Toast.makeText(this, "Без доступа к точной геолокации запись невозможна", Toast.LENGTH_LONG).show()
        }
    }

    // ---------- отправка на сервер ----------

    private fun onUpload() {
        savePrefs()
        val url = etUrl.text.toString().trim()
        val token = etToken.text.toString().trim()
        if (url.isEmpty() || token.isEmpty()) {
            Toast.makeText(this, "Укажите адрес сервера и токен", Toast.LENGTH_SHORT).show()
            return
        }
        btnUpload.isEnabled = false
        tvUpload.text = "Отправка…"
        val fromWifi = prefs.getLong("up_wifi", 0L)
        val fromBle = prefs.getLong("up_ble", 0L)
        val db = Db.get(this)
        net.execute {
            val r = Uploader.upload(db, url, token, fromWifi, fromBle)
            // Отметку «отправлено до этого id» двигаем только после успеха.
            if (r.ok) {
                prefs.edit().putLong("up_wifi", r.maxWifiId).putLong("up_ble", r.maxBleId).apply()
            }
            val line = SimpleDateFormat("dd.MM HH:mm", Locale.getDefault()).format(Date()) + " — " + r.message
            prefs.edit().putString("last_upload", line).apply()
            ui.post {
                btnUpload.isEnabled = true
                tvUpload.text = line
                Toast.makeText(this, r.message, Toast.LENGTH_LONG).show()
            }
        }
    }

    // ---------- экспорт / очистка ----------

    private fun onExport() {
        val name = "wardrive-" + SimpleDateFormat("yyyyMMdd-HHmm", Locale.US).format(Date()) + ".csv"
        val i = Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "text/csv"
            putExtra(Intent.EXTRA_TITLE, name)
        }
        startActivityForResult(i, reqExport)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != reqExport || resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        val db = Db.get(this)
        bg.execute {
            val msg = try {
                val os = contentResolver.openOutputStream(uri)
                    ?: throw IllegalStateException("не удалось открыть файл")
                Exporter.writeWigle(db, os)
                "Экспорт готов"
            } catch (e: Exception) {
                "Ошибка экспорта: ${e.message}"
            }
            ui.post { Toast.makeText(this, msg, Toast.LENGTH_LONG).show() }
        }
    }

    private fun onClear() {
        if (ScanState.running) {
            Toast.makeText(this, "Сначала остановите запись", Toast.LENGTH_SHORT).show()
            return
        }
        AlertDialog.Builder(this)
            .setTitle("Очистить базу?")
            .setMessage("Все записанные наблюдения будут удалены. Сначала сделайте экспорт, если данные нужны.")
            .setPositiveButton("Удалить") { _, _ ->
                bg.execute {
                    Db.get(this).clear()
                    ui.post { refreshUi() }
                }
            }
            .setNegativeButton("Отмена", null)
            .show()
    }

    // ---------- обновление экрана ----------

    private fun refreshUi() {
        val s = ScanState
        val fixFresh = System.currentTimeMillis() - s.lastFixAt < 10_000
        tvStatus.text = when {
            !s.running -> "Остановлено"
            fixFresh -> "Идёт запись · GPS ±${s.accuracy.toInt()} м"
            else -> "Идёт запись · ждём GPS…"
        }
        btnToggle.text = if (s.running) "Стоп" else "Старт"
        btnToggle.visibility = View.VISIBLE

        val sessionLine = if (s.running)
            "\nЗа сессию: +${s.wifi} Wi‑Fi, +${s.ble} BLE, сканов без годного GPS: ${s.noFix}"
        else ""
        bg.execute {
            val c = Db.get(this).counts()
            ui.post {
                tvStats.text = "В базе: Wi‑Fi ${c.wifiAps} точек (${c.wifiObs} наблюдений)\n" +
                    "BLE ${c.bleIds} устройств/маяков (${c.bleObs} наблюдений)" + sessionLine
            }
        }
    }
}
