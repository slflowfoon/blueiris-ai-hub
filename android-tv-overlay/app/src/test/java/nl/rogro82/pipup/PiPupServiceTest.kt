package nl.rogro82.pipup

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PiPupServiceTest {

    @Test
    fun buildExpandedPopup_centersAndDisablesTimeout() {
        val popup = PopupProps(
            duration = 15,
            position = PopupProps.Position.TopRight,
            media = PopupProps.Media.Video("rtsp://camera", width = 480, muteAudio = true),
        )

        val expanded = PiPupService.buildExpandedPopup(popup, 1920, 1080)

        assertEquals(0, expanded.duration)
        assertEquals(PopupProps.Position.Center, expanded.position)
        assertEquals(1920, (expanded.media as PopupProps.Media.Video).width)
    }

    @Test
    fun shouldAutoDismiss_falseWhenExpanded() {
        assertFalse(PiPupService.shouldAutoDismiss(PopupProps(duration = 30), expanded = true))
    }

    @Test
    fun shouldAutoDismiss_trueForCompactPopupWithDuration() {
        assertTrue(PiPupService.shouldAutoDismiss(PopupProps(duration = 30), expanded = false))
    }
}
