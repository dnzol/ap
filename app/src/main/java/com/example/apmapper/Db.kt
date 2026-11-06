package com.example.apmapper

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

class WifiObs(
    val bssid: String, val ssid: String, val caps: String, val freq: Int, val rssi: Int,
    val lat: Double, val lon: Double, val alt: Double, val acc: Float, val ts: Long
)

class BleObs(
    val mac: String, val kind: String, val beaconId: String?, val rssi: Int,
    val lat: Double, val lon: Double, val alt: Double, val acc: Float, val ts: Long
)

class Counts(val wifiObs: Long, val wifiAps: Long, val bleObs: Long, val bleIds: Long)

/** Сырые наблюдения: одна строка = «эту точку/маяк видели здесь с таким уровнем сигнала». */
class Db private constructor(ctx: Context) : SQLiteOpenHelper(ctx, "scans.db", null, 1) {

    companion object {
        @Volatile
        private var inst: Db? = null

        fun get(ctx: Context): Db =
            inst ?: synchronized(this) {
                inst ?: Db(ctx.applicationContext).also { inst = it }
            }
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """CREATE TABLE wifi_obs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bssid TEXT NOT NULL, ssid TEXT, caps TEXT, freq INTEGER, rssi INTEGER,
                lat REAL NOT NULL, lon REAL NOT NULL, alt REAL, acc REAL, ts INTEGER NOT NULL)"""
        )
        db.execSQL("CREATE INDEX idx_wifi_bssid ON wifi_obs(bssid)")
        db.execSQL(
            """CREATE TABLE ble_obs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac TEXT NOT NULL, kind TEXT, beacon_id TEXT, rssi INTEGER,
                lat REAL NOT NULL, lon REAL NOT NULL, alt REAL, acc REAL, ts INTEGER NOT NULL)"""
        )
        db.execSQL("CREATE INDEX idx_ble_mac ON ble_obs(mac)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {}

    fun insertWifi(list: List<WifiObs>) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val cv = ContentValues()
            for (o in list) {
                cv.clear()
                cv.put("bssid", o.bssid)
                cv.put("ssid", o.ssid)
                cv.put("caps", o.caps)
                cv.put("freq", o.freq)
                cv.put("rssi", o.rssi)
                cv.put("lat", o.lat)
                cv.put("lon", o.lon)
                cv.put("alt", o.alt)
                cv.put("acc", o.acc)
                cv.put("ts", o.ts)
                db.insert("wifi_obs", null, cv)
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    fun insertBle(list: List<BleObs>) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            val cv = ContentValues()
            for (o in list) {
                cv.clear()
                cv.put("mac", o.mac)
                cv.put("kind", o.kind)
                cv.put("beacon_id", o.beaconId)
                cv.put("rssi", o.rssi)
                cv.put("lat", o.lat)
                cv.put("lon", o.lon)
                cv.put("alt", o.alt)
                cv.put("acc", o.acc)
                cv.put("ts", o.ts)
                db.insert("ble_obs", null, cv)
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    fun counts(): Counts {
        val db = readableDatabase
        fun q(sql: String): Long =
            db.rawQuery(sql, null).use { if (it.moveToFirst()) it.getLong(0) else 0L }
        return Counts(
            q("SELECT COUNT(*) FROM wifi_obs"),
            q("SELECT COUNT(DISTINCT bssid) FROM wifi_obs"),
            q("SELECT COUNT(*) FROM ble_obs"),
            q("SELECT COUNT(DISTINCT COALESCE(beacon_id, mac)) FROM ble_obs")
        )
    }

    /** Наибольшие id в таблицах (wifi, ble): по ним определяем, что ещё не отправлено. */
    fun maxIds(): Pair<Long, Long> {
        val db = readableDatabase
        fun q(table: String): Long =
            db.rawQuery("SELECT COALESCE(MAX(id), 0) FROM $table", null)
                .use { if (it.moveToFirst()) it.getLong(0) else 0L }
        return q("wifi_obs") to q("ble_obs")
    }

    fun clear() {
        val db = writableDatabase
        db.execSQL("DELETE FROM wifi_obs")
        db.execSQL("DELETE FROM ble_obs")
        db.execSQL("VACUUM")
    }
}
