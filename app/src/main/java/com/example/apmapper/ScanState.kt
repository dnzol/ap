package com.example.apmapper

/** Состояние текущей сессии записи; пишется сервисом, читается экраном. */
object ScanState {
    @Volatile var running = false
    @Volatile var lastFixAt = 0L
    @Volatile var accuracy = -1f
    @Volatile var wifi = 0
    @Volatile var ble = 0
    @Volatile var noFix = 0

    fun reset() {
        lastFixAt = 0L
        accuracy = -1f
        wifi = 0
        ble = 0
        noFix = 0
    }
}
