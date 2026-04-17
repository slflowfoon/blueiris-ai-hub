package nl.rogro82.pipup

import android.view.ViewGroup
import org.junit.Assert.assertEquals
import org.junit.Test

class PopupViewTest {

    @Test
    fun scaledHeight_preservesAspectRatio() {
        assertEquals(270, PopupView.scaledHeight(480, 1920, 1080))
    }

    @Test
    fun scaledHeight_returnsWrapContentForInvalidDimensions() {
        assertEquals(
            ViewGroup.LayoutParams.WRAP_CONTENT,
            PopupView.scaledHeight(480, 0, 1080)
        )
    }
}
