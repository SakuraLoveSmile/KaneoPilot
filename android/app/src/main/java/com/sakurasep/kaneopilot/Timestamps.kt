package com.sakurasep.kaneopilot

import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

private val LOCAL_STAMP: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")

/**
 * Core timestamps are ISO 8601 with an offset. They read in the phone's zone as `yyyy-MM-dd HH:mm:ss`, the form the
 * design board uses for 记录 times. A value that does not parse is shown as the core wrote it.
 */
internal fun localStamp(value: String, zone: ZoneId = ZoneId.systemDefault()): String =
    runCatching { OffsetDateTime.parse(value).atZoneSameInstant(zone).format(LOCAL_STAMP) }.getOrDefault(value)
