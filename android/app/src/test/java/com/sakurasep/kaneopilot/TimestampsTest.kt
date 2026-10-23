package com.sakurasep.kaneopilot

import java.time.ZoneOffset
import org.junit.Assert.assertEquals
import org.junit.Test

class TimestampsTest {
    @Test
    fun coreTimestampReadsInThePhonesZone() {
        assertEquals("2026-10-22 17:07:22", localStamp("2026-10-22T09:07:22.265420+00:00", ZoneOffset.ofHours(8)))
    }

    @Test
    fun zuluSuffixIsUtc() {
        assertEquals("2026-10-22 09:07:22", localStamp("2026-10-22T09:07:22Z", ZoneOffset.UTC))
    }

    @Test
    fun unparsedValueIsShownAsWritten() {
        assertEquals("", localStamp(""))
        assertEquals("昨天", localStamp("昨天"))
    }
}
