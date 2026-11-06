package com.example.apmapper

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothManager
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanRecord
import android.bluetooth.le.ScanResult as BleResult
import android.bluetooth.le.ScanSettings
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.ParcelUuid
import android.os.PowerManager
import android.os.SystemClock
import java.util.Locale
import java.util.concurrent.Executors

private val EDDYSTONE: ParcelUuid = ParcelUuid.fromString("0000FEAA-0000-1000-8000-00805F9B34FB")

/**
 * Foreground-сервис: берёт GPS-координату и при каждом результате скана Wi-Fi / BLE
 * пишет в базу строки «точка + уровень сигнала + где я стоял».
 */
class ScanService : Service() {

    private class Last(val ts: Long, val lat: Double, val lon: Double)

    private lateinit var wifi: WifiManager
    private lateinit var lm: LocationManager
    private lateinit var db: Db
    private lateinit var prefs: SharedPreferences

    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private val lastWritten = HashMap<String, Last>()

    private var lastLoc: Location? = null
    private var minAcc = 30f
    private var wifiOn = true
    private var bleOn = true
    private var beaconsOnly = true

    private var bleScanner: BluetoothLeScanner? = null
    private var receiverRegistered = false
    private var wakeLock: PowerManager.WakeLock? = null
    private var started = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        lm = getSystemService(Context.LOCATION_SERVICE) as LocationManager
        db = Db.get(this)
        prefs = getSharedPreferences("prefs", MODE_PRIVATE)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (started) return START_NOT_STICKY
        started = true

        goForeground()

        minAcc = prefs.getInt("min_acc", 30).toFloat()
        wifiOn = prefs.getBoolean("wifi", true)
        bleOn = prefs.getBoolean("ble", true)
        beaconsOnly = prefs.getBoolean("beacons_only", true)

        ScanState.reset()
        ScanState.running = true

        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "apmapper:scan").also { it.acquire() }

        startLocation()
        if (wifiOn) startWifi()
        if (bleOn) startBle()
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        try { lm.removeUpdates(locListener) } catch (_: Exception) {}
        if (receiverRegistered) {
            try { unregisterReceiver(wifiReceiver) } catch (_: Exception) {}
            receiverRegistered = false
        }
        try { bleScanner?.stopScan(bleCb) } catch (_: Exception) {}
        main.removeCallbacksAndMessages(null)
        wakeLock?.let { if (it.isHeld) it.release() }
        io.shutdown()
        ScanState.running = false
        super.onDestroy()
    }

    // ---------- уведомление ----------

    private fun goForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel("scan", "Сканирование", NotificationManager.IMPORTANCE_LOW)
        )
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        val n = Notification.Builder(this, "scan")
            .setContentTitle("AP Mapper")
            .setContentText("Идёт запись Wi‑Fi / BLE точек")
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setContentIntent(pi)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION)
        } else {
            startForeground(1, n)
        }
    }

    // ---------- GPS ----------

    private val locListener = object : LocationListener {
        override fun onLocationChanged(location: Location) {
            lastLoc = location
            ScanState.lastFixAt = System.currentTimeMillis()
            ScanState.accuracy = location.accuracy
        }

        override fun onProviderEnabled(provider: String) {}
        override fun onProviderDisabled(provider: String) {}

        @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
        override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
    }

    @SuppressLint("MissingPermission")
    private fun startLocation() {
        try {
            // Только GPS: «сетевая» локация сама считается по Wi-Fi, получился бы замкнутый круг.
            lm.requestLocationUpdates(
                LocationManager.GPS_PROVIDER, 1000L, 0f, locListener, Looper.getMainLooper()
            )
        } catch (e: SecurityException) {
            stopSelf()
        }
    }

    /** Свежая и достаточно точная координата, иначе null (такие наблюдения не пишем). */
    private fun goodLoc(): Location? {
        val l = lastLoc ?: return null
        val ageMs = (SystemClock.elapsedRealtimeNanos() - l.elapsedRealtimeNanos) / 1_000_000
        if (ageMs > 10_000) return null
        if (!l.hasAccuracy() || l.accuracy > minAcc) return null
        return l
    }

    /** Не пишем одну и ту же точку чаще раза в 30 с, если мы стоим на месте. */
    private fun due(key: String, now: Long, loc: Location): Boolean {
        val l = lastWritten[key]
        if (l != null) {
            val d = FloatArray(1)
            Location.distanceBetween(l.lat, l.lon, loc.latitude, loc.longitude, d)
            if (now - l.ts < 30_000 && d[0] < 10f) return false
        }
        lastWritten[key] = Last(now, loc.latitude, loc.longitude)
        return true
    }

    // ---------- Wi-Fi ----------

    private val wifiReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            handleWifi()
        }
    }

    private val wifiTick = object : Runnable {
        @Suppress("DEPRECATION")
        override fun run() {
            // Android ограничивает частоту (≈4 скана / 2 мин); лишние вызовы просто вернут false.
            try { wifi.startScan() } catch (_: Exception) {}
            main.postDelayed(this, 8_000)
        }
    }

    private fun startWifi() {
        val f = IntentFilter(WifiManager.SCAN_RESULTS_AVAILABLE_ACTION)
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(wifiReceiver, f, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(wifiReceiver, f)
        }
        receiverRegistered = true
        main.post(wifiTick)
    }

    @SuppressLint("MissingPermission")
    private fun handleWifi() {
        val results = try { wifi.scanResults } catch (e: SecurityException) { return }
        val loc = goodLoc()
        if (loc == null) {
            ScanState.noFix++
            return
        }
        val now = System.currentTimeMillis()
        val nowBoot = SystemClock.elapsedRealtime()
        val alt = if (loc.hasAltitude()) loc.altitude else 0.0
        val out = ArrayList<WifiObs>()
        for (r in results) {
            val bssid = r.BSSID?.lowercase(Locale.ROOT) ?: continue
            // Кэшированные старые результаты не привязываем к новой координате.
            if (nowBoot - r.timestamp / 1000 > 15_000) continue
            @Suppress("DEPRECATION")
            val ssid = r.SSID ?: ""
            // Владелец сети просит не заносить её в карты.
            if (ssid.lowercase(Locale.ROOT).endsWith("_nomap")) continue
            if (!due("w:$bssid", now, loc)) continue
            out += WifiObs(
                bssid, ssid, r.capabilities ?: "", r.frequency, r.level,
                loc.latitude, loc.longitude, alt, loc.accuracy, now
            )
        }
        if (out.isNotEmpty()) {
            ScanState.wifi += out.size
            io.execute { db.insertWifi(out) }
        }
    }

    // ---------- Bluetooth LE ----------

    private val bleCb = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: BleResult) {
            handleBle(listOf(result))
        }

        override fun onBatchScanResults(results: MutableList<BleResult>) {
            handleBle(results)
        }
    }

    @SuppressLint("MissingPermission")
    private fun startBle() {
        if (Build.VERSION.SDK_INT >= 31 &&
            checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) != PackageManager.PERMISSION_GRANTED
        ) return
        val adapter = (getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter
        if (adapter == null || !adapter.isEnabled) return
        val sc = adapter.bluetoothLeScanner ?: return

        // С выключенным экраном Android отдаёт только отфильтрованные результаты,
        // поэтому в режиме «только маяки» ставим аппаратные фильтры.
        val filters: List<ScanFilter>? = if (beaconsOnly) {
            listOf(
                ScanFilter.Builder()
                    .setManufacturerData(
                        0x004C,
                        byteArrayOf(0x02, 0x15),
                        byteArrayOf(0xFF.toByte(), 0xFF.toByte())
                    ).build(),
                ScanFilter.Builder().setServiceUuid(EDDYSTONE).build()
            )
        } else null

        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_BALANCED).build()
        try {
            sc.startScan(filters, settings, bleCb)
            bleScanner = sc
        } catch (_: Exception) {
        }
    }

    private fun handleBle(list: List<BleResult>) {
        val loc = goodLoc() ?: return
        val now = System.currentTimeMillis()
        val alt = if (loc.hasAltitude()) loc.altitude else 0.0
        val out = ArrayList<BleObs>()
        for (r in list) {
            val mac = r.device.address?.lowercase(Locale.ROOT) ?: continue
            val beacon = parseBeacon(r.scanRecord)
            if (beacon == null) {
                if (beaconsOnly) continue
                // Случайные (приватные) адреса телефонов, часов и т.п. не пишем.
                if (isLocallyAdministered(mac)) continue
            }
            val key = "b:" + (beacon?.second ?: mac)
            if (!due(key, now, loc)) continue
            out += BleObs(
                mac, beacon?.first ?: "other", beacon?.second, r.rssi,
                loc.latitude, loc.longitude, alt, loc.accuracy, now
            )
        }
        if (out.isNotEmpty()) {
            ScanState.ble += out.size
            io.execute { db.insertBle(out) }
        }
    }

    private fun isLocallyAdministered(mac: String): Boolean =
        (mac.substring(0, 2).toInt(16) and 0x02) != 0

    /** Возвращает (тип, стабильный id) для iBeacon / Eddystone-UID, иначе null. */
    private fun parseBeacon(rec: ScanRecord?): Pair<String, String>? {
        if (rec == null) return null

        val m = rec.getManufacturerSpecificData(0x004C)
        if (m != null && m.size >= 22 && m[0] == 0x02.toByte() && m[1] == 0x15.toByte()) {
            val uuid = hex(m, 2, 16)
            val major = ((m[18].toInt() and 0xFF) shl 8) or (m[19].toInt() and 0xFF)
            val minor = ((m[20].toInt() and 0xFF) shl 8) or (m[21].toInt() and 0xFF)
            return "ibeacon" to "ibeacon:$uuid:$major:$minor"
        }

        val s = rec.getServiceData(EDDYSTONE)
        if (s != null && s.size >= 18 && s[0] == 0x00.toByte()) {
            return "eddystone" to "eddystone:" + hex(s, 2, 10) + ":" + hex(s, 12, 6)
        }
        return null
    }

    private fun hex(b: ByteArray, off: Int, len: Int): String = buildString {
        for (i in off until off + len) append("%02x".format(b[i]))
    }
}
