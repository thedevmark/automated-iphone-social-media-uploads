import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from scripts import phone_instagram as ig
from video_drop.core import Store
from video_drop.phone_ui import PhoneLayout
from video_drop.screens.snapshot import Element

CAPTION = "Example title #example #reels"
TARGETS = {"instagram": "@creator", "facebook": "1234567890", "threads": "@creator"}


def release(folder: str, *, mode: str = "post_now", crossposts=("facebook", "threads")) -> tuple[Store, int]:
    source = Path(folder) / "clip.mp4"
    source.write_bytes(b"finished source")
    store = Store(Path(folder) / "release.sqlite", TARGETS)
    release_id = store.import_file(source)["id"]
    store.set_delivery_mode(release_id, mode)
    store.save_text(release_id, "instagram", "@creator", "Example title", CAPTION, "")
    store.authorize(release_id, "instagram")
    for platform in crossposts:
        store.save_text(release_id, platform, TARGETS[platform], "", "", "")
        store.authorize(release_id, platform)
    return store, release_id


@mock.patch.object(ig, "edits_color_mode", lambda source: "SDR")
@mock.patch.object(ig, "source_frame_rate", lambda source: 59.94)
class InputTests(unittest.TestCase):
    def test_post_now_with_both_crossposts_approved_is_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            store, release_id = release(folder)
            with store:
                data = ig.release_input(store, release_id)
        self.assertEqual((data["caption"], data["color"], data["fps"]), (CAPTION, "SDR", "60"))

    def test_post_now_always_crossposts_so_both_must_be_approved(self):
        for missing in ("facebook", "threads"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as folder:
                store, release_id = release(folder, crossposts=tuple(p for p in ("facebook", "threads") if p != missing))
                with store, self.assertRaisesRegex(ig.Stop, f"{missing.title()} text is not approved"):
                    ig.release_input(store, release_id)

    def test_crossposts_follow_the_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            store, release_id = release(folder, crossposts=("facebook",))
            with store:
                store.set_phone_checks({"crosspostThreads": False})
                data = ig.release_input(store, release_id)
        # Threads is off in Settings: not required, not carried, left for its own path.
        self.assertEqual(data["crossposts"], ["facebook"])

    def test_schedule_needs_a_reserved_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            store, release_id = release(folder, mode="schedule")
            with store, self.assertRaisesRegex(ig.Stop, "Reserve a future slot"):
                ig.release_input(store, release_id)

    def test_an_attempted_instagram_is_never_started_again(self):
        with tempfile.TemporaryDirectory() as folder:
            store, release_id = release(folder)
            with store:
                store.mark_unconfirmed(release_id, "instagram")
                with self.assertRaisesRegex(ig.Stop, "already attempted"):
                    ig.release_input(store, release_id)


class ExportSettingsTests(unittest.TestCase):
    def test_frame_rate_follows_the_source_and_never_exceeds_it(self):
        self.assertEqual([ig.edits_frame_rate(fps) for fps in (59.94, 60, 50, 30, 29.97, 25, 24)],
                         ["60", "60", "60", "30", "30", "24", "24"])

    def test_selected_segment_is_the_white_pill(self):
        # Recorded 2026-09-30: the chosen Edits segment is a white pill on a dark popover.
        layout = PhoneLayout(440, 956)
        image = Image.new("RGB", (1320, 2868), (40, 40, 44))
        ImageDraw.Draw(image).rectangle((340 * 3, 144 * 3, 408 * 3, 176 * 3), fill=(250, 250, 250))
        chosen = Element("Button", "4K", "video_quality_segment_2_4K", "", 340, 144, 68, 32)
        other = Element("Button", "2K", "video_quality_segment_1_2K", "", 272, 144, 68, 32)
        with mock.patch.object(ig.share, "layout", lambda refresh=False: layout):
            self.assertTrue(ig.selected(image, chosen))
            self.assertFalse(ig.selected(image, other))


class CoverTests(unittest.TestCase):
    """The composer's card shows the centered first frame between the Preview and Edit cover pills."""

    CARD = Element("Other", "Edit cover", "Edit cover", "", 145, 140, 150, 264)
    PREVIEW = Element("Button", "Preview", "Preview", "", 155, 150, 87, 31)
    EDIT = Element("Button", "Edit cover", "Edit cover", "", 156, 362, 101, 31)

    @staticmethod
    def frame(seed: int) -> Image.Image:
        image = Image.new("RGB", (216, 384), (20 + seed * 30, 30, 60))
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 80 + seed * 40, 180, 160 + seed * 40), fill=(230, 200, 40))
        draw.ellipse((70, 200, 150, 300), fill=(200, 120 + seed * 20, 90))
        return image

    def composer_with(self, frame: Image.Image) -> Image.Image:
        screen = Image.new("RGB", (1320, 2868), (255, 255, 255))
        card = self.CARD
        screen.paste(frame.resize((round(card.width * 3), round(card.height * 3))), (round(card.left * 3), round(card.top * 3)))
        return screen

    def test_first_frame_passes_and_a_later_frame_fails(self):
        first, later = self.frame(0), self.frame(3)
        with mock.patch.object(ig.share, "layout", lambda refresh=False: PhoneLayout(440, 956)):
            same = ig.cover_score(self.composer_with(first), first, self.CARD, self.PREVIEW, self.EDIT)
            different = ig.cover_score(self.composer_with(later), first, self.CARD, self.PREVIEW, self.EDIT)
        self.assertLess(same, ig.COVER_LIMIT)
        self.assertGreater(different, ig.COVER_LIMIT)


if __name__ == "__main__":
    unittest.main()


def test_the_edits_share_sheet_read_by_ocr_taps_the_one_instagram_target():
    # Captured 2026-10-02 12:28: the share sheet plays the clip, so its tree is refused.
    from unittest.mock import patch
    from scripts import phone_instagram as ig
    from video_drop.phone_ui import PhoneLayout
    rows = [{"text": "Choose where to share", "x": 220, "y": 470},
            {"text": "Videos are optimized for high-quality playback", "x": 220, "y": 500},
            {"text": "on Instagram.", "x": 220, "y": 520},
            {"text": "Instagram", "x": 60, "y": 820}, {"text": "Facebook", "x": 140, "y": 820},
            {"text": "Stories", "x": 220, "y": 820}]
    with patch.object(ig.share, "layout", lambda **k: PhoneLayout(440, 956)):
        assert ig.ocr_share_target(rows) == {"text": "Instagram", "x": 60, "y": 820}
        assert ig.ocr_share_target([{"text": "Exporting", "x": 1, "y": 1}]) is None
        # Real Post now 2026-10-03 23:05: the export's progress screen, 27.6% done.
        progress = [{"text": "27.6%", "x": 185, "y": 146},
                    {"text": "Please don't close the app or lock your screen. You can", "x": 42, "y": 180},
                    {"text": "choose where to share your video next.", "x": 94, "y": 199}]
        assert ig.ocr_share_target(progress) is None
