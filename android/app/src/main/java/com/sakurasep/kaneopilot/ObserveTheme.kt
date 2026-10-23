package com.sakurasep.kaneopilot

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/** 观测台扩展令牌 `observeLane.*`（安卓契约「视觉与字体」，与 ui-spec §4.18「类别色」同源）。
 * 颜色只在此统一定义；视图通过 [observeLanes] 取用，不写死十六进制。 */
data class ObserveLanePalette(
    val plan: Color,
    val dispatch: Color,
    val execute: Color,
    val waiting: Color,
    val repair: Color,
    val review: Color,
    val other: Color,
) {
    fun of(lane: String): Color = when (lane) {
        "plan" -> plan
        "dispatch" -> dispatch
        "execute" -> execute
        "waiting" -> waiting
        "repair" -> repair
        "review" -> review
        else -> other
    }
}

private val LightObserveLanes = ObserveLanePalette(
    plan = Color(0xFF007AFF), dispatch = Color(0xFF8E8E93), execute = Color(0xFFAF52DE), waiting = Color(0xFFFF9500),
    repair = Color(0xFFA2845E), review = Color(0xFF30B0C7), other = Color(0xFFC7C7CC),
)

private val DarkObserveLanes = ObserveLanePalette(
    plan = Color(0xFF0A84FF), dispatch = Color(0xFF98989D), execute = Color(0xFFBF5AF2), waiting = Color(0xFFFF9F0A),
    repair = Color(0xFFAC8E68), review = Color(0xFF40C8E0), other = Color(0xFF48484A),
)

@Composable
fun observeLanes(): ObserveLanePalette = if (isSystemInDarkTheme()) DarkObserveLanes else LightObserveLanes
