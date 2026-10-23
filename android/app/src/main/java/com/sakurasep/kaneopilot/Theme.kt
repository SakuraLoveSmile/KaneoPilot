package com.sakurasep.kaneopilot

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color

/** Semantic colours that Material 3 has no role for. Tokens follow contract 「视觉与状态」. */
data class KaneoStatusColors(val success: Color, val successContainer: Color, val onSuccessContainer: Color)

private val lightStatusColors = KaneoStatusColors(Color(0xFF17693A), Color(0xFFC9EFD3), Color(0xFF002110))
private val darkStatusColors = KaneoStatusColors(Color(0xFF7FD99C), Color(0xFF0B3F20), Color(0xFFC9EFD3))

val LocalKaneoStatusColors = staticCompositionLocalOf { lightStatusColors }

// Every role is listed, so no baseline colour leaks in. Fixed roles are the same in both themes by definition.
private val lightScheme = lightColorScheme(
    primary = Color(0xFF2456A8), onPrimary = Color(0xFFFFFFFF),
    primaryContainer = Color(0xFFD9E2FF), onPrimaryContainer = Color(0xFF001B45),
    inversePrimary = Color(0xFFA9C6FF), surfaceTint = Color(0xFF2456A8),
    secondary = Color(0xFF565E71), onSecondary = Color(0xFFFFFFFF),
    secondaryContainer = Color(0xFFDBE2F9), onSecondaryContainer = Color(0xFF141B2C),
    tertiary = Color(0xFF8A4B00), onTertiary = Color(0xFFFFFFFF),
    tertiaryContainer = Color(0xFFFFDCC0), onTertiaryContainer = Color(0xFF2B1600),
    error = Color(0xFFB3261E), onError = Color(0xFFFFFFFF),
    errorContainer = Color(0xFFF9DEDC), onErrorContainer = Color(0xFF410E0B),
    background = Color(0xFFF8F9FF), onBackground = Color(0xFF191C22),
    surface = Color(0xFFF8F9FF), onSurface = Color(0xFF191C22),
    surfaceVariant = Color(0xFFE1E2EC), onSurfaceVariant = Color(0xFF434751),
    surfaceDim = Color(0xFFD8DAE2), surfaceBright = Color(0xFFF8F9FF),
    surfaceContainerLowest = Color(0xFFFFFFFF), surfaceContainerLow = Color(0xFFF2F3FA),
    surfaceContainer = Color(0xFFECEEF6), surfaceContainerHigh = Color(0xFFE6E8F0),
    surfaceContainerHighest = Color(0xFFE0E2EA),
    outline = Color(0xFF737782), outlineVariant = Color(0xFFC3C6D2),
    inverseSurface = Color(0xFF2E3038), inverseOnSurface = Color(0xFFF0F0F7),
    scrim = Color(0xFF000000),
    primaryFixed = Color(0xFFD9E2FF), primaryFixedDim = Color(0xFFA9C6FF),
    onPrimaryFixed = Color(0xFF001B45), onPrimaryFixedVariant = Color(0xFF00438F),
    secondaryFixed = Color(0xFFDBE2F9), secondaryFixedDim = Color(0xFFBFC6DC),
    onSecondaryFixed = Color(0xFF141B2C), onSecondaryFixedVariant = Color(0xFF3F4759),
    tertiaryFixed = Color(0xFFFFDCC0), tertiaryFixedDim = Color(0xFFFFB870),
    onTertiaryFixed = Color(0xFF2B1600), onTertiaryFixedVariant = Color(0xFF4A2800),
)

private val darkScheme = darkColorScheme(
    primary = Color(0xFFA9C6FF), onPrimary = Color(0xFF002F68),
    primaryContainer = Color(0xFF00438F), onPrimaryContainer = Color(0xFFD9E2FF),
    inversePrimary = Color(0xFF2456A8), surfaceTint = Color(0xFFA9C6FF),
    secondary = Color(0xFFBFC6DC), onSecondary = Color(0xFF293041),
    secondaryContainer = Color(0xFF3F4759), onSecondaryContainer = Color(0xFFDBE2F9),
    tertiary = Color(0xFFFFB870), onTertiary = Color(0xFF4A2800),
    tertiaryContainer = Color(0xFF4A2800), onTertiaryContainer = Color(0xFFFFDCC0),
    error = Color(0xFFFFB4AB), onError = Color(0xFF690005),
    errorContainer = Color(0xFF93000A), onErrorContainer = Color(0xFFFFDAD6),
    background = Color(0xFF111318), onBackground = Color(0xFFE2E2E9),
    surface = Color(0xFF111318), onSurface = Color(0xFFE2E2E9),
    surfaceVariant = Color(0xFF43474E), onSurfaceVariant = Color(0xFFC3C6D2),
    surfaceDim = Color(0xFF111318), surfaceBright = Color(0xFF37393E),
    surfaceContainerLowest = Color(0xFF0C0E13), surfaceContainerLow = Color(0xFF191C20),
    surfaceContainer = Color(0xFF1D2026), surfaceContainerHigh = Color(0xFF282A2F),
    surfaceContainerHighest = Color(0xFF32353C),
    outline = Color(0xFF8D909C), outlineVariant = Color(0xFF43474E),
    inverseSurface = Color(0xFFE2E2E9), inverseOnSurface = Color(0xFF2E3036),
    scrim = Color(0xFF000000),
    primaryFixed = Color(0xFFD9E2FF), primaryFixedDim = Color(0xFFA9C6FF),
    onPrimaryFixed = Color(0xFF001B45), onPrimaryFixedVariant = Color(0xFF00438F),
    secondaryFixed = Color(0xFFDBE2F9), secondaryFixedDim = Color(0xFFBFC6DC),
    onSecondaryFixed = Color(0xFF141B2C), onSecondaryFixedVariant = Color(0xFF3F4759),
    tertiaryFixed = Color(0xFFFFDCC0), tertiaryFixedDim = Color(0xFFFFB870),
    onTertiaryFixed = Color(0xFF2B1600), onTertiaryFixedVariant = Color(0xFF4A2800),
)

/** Follows the system light or dark setting; dynamic colour is not used, so brand and status colours stay fixed. */
@Composable
fun KaneoTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    CompositionLocalProvider(LocalKaneoStatusColors provides if (dark) darkStatusColors else lightStatusColors) {
        MaterialTheme(colorScheme = if (dark) darkScheme else lightScheme, content = content)
    }
}
