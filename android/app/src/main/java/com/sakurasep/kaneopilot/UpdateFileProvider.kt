package com.sakurasep.kaneopilot

import androidx.core.content.FileProvider

/** Grants the system installer read access to the update cache, never pairing or application files. */
class UpdateFileProvider : FileProvider()
