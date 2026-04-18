package io.slflowfoon.blueirisaihub.tv

import com.fasterxml.jackson.core.JsonFactory
import com.fasterxml.jackson.databind.ObjectMapper
import com.fasterxml.jackson.module.kotlin.KotlinModule

object Json : ObjectMapper(JsonFactory()) {
    init {
        registerModule(KotlinModule())
    }
}