"""Post or schedule one approved release on Instagram through Edits' 4K export.

The only Instagram route (owner's rule, 2026-09-30): OneDrive -> Edits -> new project from the
exact clip -> 4K export at the source frame rate and the preflight's SDR/HDR -> share to
Instagram from Edits -> verify account, caption, first-frame cover and the Facebook + Threads
crossposts -> one Share tap. Post now always crossposts to Facebook and Threads from this one
upload; Threads is never posted separately for a Post now release.

Recorded on the reference iPhone 16 Pro Max, iOS 26.7 (fixtures edits/*, instagram/*
2026-09-30). Every step runs in ONE WebDriverAgent session: a new session re-activates
Instagram and pops its "Also share on" page. Nothing is read while a video plays: after Share,
Instagram opens its autoplaying feed, so the run leaves for the Home Screen over USB.
Without --commit, stop at the fully verified composer.

Schedule mode (the default delivery mode) runs the same Edits export and composer checks,
then More options -> "Schedule this reel" -> the "Schedule reel" sheet. Its Date and Time
rows are drawn but not in the tree (recording schedule-on), so they are placed from the live
Done button and every tap is proven by the next screen: the calendar popover for Date (day
buttons, recording schedule-date), the time wheels for Time (never recorded: anything but
the iOS hour/minute/AM-PM wheels stops with "time picker not mapped yet"). Both values are
read back by re-opening them. Scheduling turns the Threads crosspost off ("1 profile,
2 unavailable"); Threads is scheduled separately in the Threads app. Facebook must be on.
The final button then reads "Schedule"; Instagram and Facebook are marked unconfirmed before
it. The Scheduled content receipt screen is not recorded yet, so the result stays unconfirmed.
"""

from __future__ import annotations

import argparse
import json
import re
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from video_drop import phone_lock  # noqa: E402
from video_drop import ocr  # noqa: E402
from video_drop.phone.helpers import VideoSurfaceError  # noqa: E402

from video_drop import native_schedule as ns  # noqa: E402
from video_drop import receipts  # noqa: E402
from video_drop.core import Store, digest  # noqa: E402
from video_drop.accounts import load_targets, require_target  # noqa: E402
from video_drop.instagram_schedule import first_frame  # noqa: E402
from video_drop.media_color import edits_color_mode  # noqa: E402
from video_drop.phone_focus import optional_focus, run_guards  # noqa: E402
from video_drop.phone_link import release_frozen_app  # noqa: E402
from video_drop.screens.snapshot import Element, elements_from_tree  # noqa: E402
from scripts import phone_youtube as share  # noqa: E402
from scripts import phone_instagram_preflight as preflight  # noqa: E402

phone = share.phone
EDITS_BUNDLE = "com.burbn.basel"
INSTAGRAM_BUNDLE = "com.burbn.instagram"
CAPTION_ID = "caption-cell-text-view"
SHARE_ID = "share-sheet-share-button"
CROSSPOSTS = {"threads": "Threads", "facebook": "Facebook"}
# Instagram's scheduler turns the Threads crosspost off; a scheduled reel carries Facebook only.
SCHEDULE_CROSSPOSTS = {"facebook": "Facebook"}
SHEET_TIMEOUT = 10.0
TIME_WHEELS_TIMEOUT = 6.0
COVER_LIMIT = 10.0  # true first frame 7.1, nearest wrong frame 13.7 (device, 2026-09-30)
EXPORT_TIMEOUT = 900.0


class Stop(share.PhoneUploadError):
    pass


# ---- inputs ------------------------------------------------------------------


def _approved(store: Store, release: dict, platform: str) -> dict:
    destination = next(d for d in release["destinations"] if d["platform"] == platform)
    if destination["status"] != "pending":
        raise Stop(f"{platform.title()} is already attempted; check its account before any retry")
    if not destination["revision_hash"]:
        raise Stop(f"{platform.title()} text is not approved in Auto iPhone Uploader")
    revision = store._revision_hash(platform, destination["account"], destination["title"],
                                    destination["description"], destination["tags"], destination["visibility"])
    if revision != destination["revision_hash"]:
        raise Stop(f"{platform.title()} text changed after approval")
    return destination


def source_frame_rate(source: Path) -> float:
    done = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=avg_frame_rate", "-of", "csv=p=0", str(source)],
                          capture_output=True, text=True, timeout=60)
    num, _, den = done.stdout.strip().partition("/")
    return float(num) / float(den or 1)


def edits_frame_rate(fps: float) -> str:
    """Edits offers 24, 30 and 60; export at the source rate, never above it."""
    return "60" if fps >= 50 else "30" if fps >= 29 else "24"


def release_input(store: Store, release_id: int, now: datetime | None = None) -> dict:
    release = store.release(release_id)
    mode = release["delivery_mode"]
    instagram = _approved(store, release, "instagram")
    require_target(store.account_targets, "instagram", instagram["account"])
    # Post now: one Instagram upload carries the crossposts Settings leave on (release
    # "instagramCrossposts", default Facebook + Threads). Schedule: Instagram's scheduler carries
    # Facebook only; Threads is scheduled in Threads.
    carried = (list(release.get("instagramCrossposts", CROSSPOSTS)) if mode == "post_now"
               else list(SCHEDULE_CROSSPOSTS))
    for platform in carried:
        _approved(store, release, platform)
    schedule = {}
    if mode == "schedule":
        if not release["scheduled_at"]:
            raise Stop("Reserve a future slot before scheduling Instagram; nothing was posted")
        # Owner's rule: a scheduled reel carries the Facebook crosspost. If Settings turned it off,
        # core would not claim Facebook with Instagram's tap; stop before the phone instead.
        if "facebook" not in release.get("instagramCrossposts", ["facebook"]):
            raise Stop("Schedule mode needs the Facebook crosspost on; turn it on in Settings. Nothing was posted")
        try:
            moment = ns.slot_on_phone(release["scheduled_at"], store.time_zone(),
                                      (now or datetime.now(timezone.utc)) + ns.MIN_LEAD)
        except ValueError as exc:
            raise Stop(f"{exc} (at least {ns.MIN_LEAD.seconds // 60} minutes ahead). Nothing was posted") from exc
        schedule = {"scheduledAt": release["scheduled_at"], "moment": moment}
    elif mode != "post_now":
        raise Stop(f"Unknown delivery mode {mode!r}; nothing was posted")
    source = Path(release["source_path"])
    if source.name != release["source_name"] or not source.is_file() or source.stat().st_size != release["file_size"]:
        raise Stop("Source filename or size changed")
    if digest(source) != release["sha256"]:
        raise Stop("Source content changed")
    caption = instagram["description"] or instagram["title"]
    if not caption:
        raise Stop("Instagram caption is empty")
    return {"releaseId": release_id, "filename": source.name, "sizeBytes": release["file_size"],
            "sourcePath": str(source), "account": instagram["account"], "caption": caption,
            "color": edits_color_mode(source), "fps": edits_frame_rate(source_frame_rate(source)),
            "revisionHash": instagram["revision_hash"], "mode": mode, "crossposts": carried, **schedule}


# ---- screen reads (one session, no app polling on video) ----------------------


def elements() -> tuple[Element, ...]:
    # Waits out a playing clip (Edits, the composer's preview) instead of asking WDA there; the
    # driver refuses that read, and a screen that keeps playing stops the run (VideoSurfaceError).
    return elements_from_tree(share.still_tree(driver=phone))


def by_name(items, name: str, kind: str | None = None) -> Element:
    found = {(e.left, e.top, e.width, e.height): e for e in items if e.name == name and (kind is None or e.type == kind)}
    if len(found) != 1:
        raise Stop(f"Expected one {name!r} on the phone, found {len(found)}; nothing was posted")
    return next(iter(found.values()))


def by_label(items, label: str, kind: str, *, prefix: bool = False) -> Element:
    found = {(e.left, e.top, e.width, e.height): e for e in items if e.type == kind
             and (e.label.startswith(label) if prefix else e.label == label)}
    if len(found) != 1:
        raise Stop(f"Expected one {label!r} {kind} on the phone, found {len(found)}; nothing was posted")
    return next(iter(found.values()))


def wait_for(name: str, timeout: float = 30.0, poll: float = 1.5) -> tuple[Element, ...]:
    deadline = time.monotonic() + timeout
    while True:
        items = elements()
        if any(e.name == name for e in items):
            return items
        if time.monotonic() >= deadline:
            raise Stop(f"The phone did not show {name!r} in time; nothing was posted")
        time.sleep(poll)


def tap(element: Element, settle: float = 1.5) -> None:
    phone.tap(element.x, element.y)
    time.sleep(settle)


def screen_image() -> Image.Image:
    # go-ios pixels, never WDA's /screenshot: that queues behind a snapshot a playing preview
    # hangs, and the cover proof and Edits' segment check run on screens that show the clip.
    return Image.open(BytesIO(share.screen_pixels())).convert("RGB")


def selected(image: Image.Image, element: Element) -> bool:
    """Edits marks the chosen segment with a white pill; the others stay dark."""
    sx, sy = image.width / share.layout().width, image.height / share.layout().height
    r, g, b = image.getpixel((int((element.left + 6) * sx), int(element.y * sy)))
    return min(r, g, b) > 200


# ---- Edits -------------------------------------------------------------------


def edits_export(data: dict) -> None:
    """New Edits project from the exact clip, set 4K / source fps / SDR-HDR, export, reach the share targets."""
    items = wait_for("project_navigation_export_button")
    quality_button = by_name(items, "project_navigation_video_quality_button", "Button")
    tap(quality_button)
    items = wait_for("video_quality_segment_2_4K")
    wanted = [by_name(items, "video_quality_segment_2_4K", "Button"),
              next(e for e in items if e.name.startswith("video_quality_segment_") and e.label == data["fps"]),
              by_name(items, "video_quality_segment_0_SDR" if data["color"] == "SDR" else "video_quality_segment_1_HDR",
                      "Button")]
    for choice in wanted:
        tap(choice, 1.0)
    image = screen_image()
    missing = [choice.label for choice in wanted if not selected(image, choice)]
    if missing:
        raise Stop(f"Edits export settings did not take: {', '.join(missing)}; nothing was exported")
    # The popover hides the header; tapping the quality button's place closes it.
    tap(quality_button)
    items = wait_for("project_navigation_export_button")
    if by_name(items, "project_navigation_video_quality_button", "Button").label != "4K":
        raise Stop("Edits did not keep 4K; nothing was exported")
    share.stage("edits_export")
    tap(by_name(items, "project_navigation_export_button", "Button"), 3)
    # Exporting: wait without hammering WDA ("don't close the app or lock your screen").
    deadline = time.monotonic() + EXPORT_TIMEOUT
    while True:
        time.sleep(10)
        try:
            items = elements()
        except VideoSurfaceError:
            # The finished export's share sheet plays the clip (captured 2026-10-02 12:28), so its
            # tree is refused: read it by OCR and tap the one "Instagram" share target instead.
            target = ocr_share_target()
            if target is not None:
                share.stage("edits_exported")
                phone.tap(target["x"], target["y"])
                time.sleep(6)
                return
            items = ()
        if any(e.name == "bsl_export_share_instagram_button" for e in items):
            break
        if time.monotonic() >= deadline:
            raise Stop("Edits export did not finish; nothing was posted")
    share.stage("edits_exported")
    tap(by_name(items, "bsl_export_share_instagram_button", "Button"), 6)


SHARE_SHEET = "choose where to share"


def ocr_share_target(rows: list[dict] | None = None) -> dict | None:
    """Edits' share sheet read by OCR: the single exact "Instagram" target in the lower half,
    or None while the sheet is not up. The heading's "...on Instagram." never matches exactly."""
    if rows is None:
        rows = ocr.screen_rows(share.screen_pixels(), share.layout().width)
    texts = [" ".join(str(row.get("text", "")).split()).casefold() for row in rows]
    # The export's progress screen says "...You can / choose where to share your video next."
    # beside its percentage; OCR wraps that second line so it also starts with the heading
    # (real Post now 2026-10-03 23:05, at 27.6%). A percentage or that line means: exporting.
    if any(re.fullmatch(r"\d{1,3}(\.\d)?%", text) or text.startswith("please don't close the app")
           or text.startswith(SHARE_SHEET + " your video next") for text in texts):
        return None
    if not any(text.startswith(SHARE_SHEET) for text in texts):
        return None
    targets = [row for row, text in zip(rows, texts)
               if text == "instagram" and row["y"] > share.layout().height / 2]
    if len(targets) != 1:
        raise Stop(f"Edits' share sheet shows {len(targets)} Instagram targets; nothing was posted")
    return targets[0]


# ---- Instagram composer ---------------------------------------------------------


def cover_score(image: Image.Image, frame: Image.Image, card: Element, preview: Element, edit: Element) -> float:
    """Mean difference between the composer's cover card and the clip's first frame (0-255).

    The card (the 'Edit cover' Other element) is filled with the centered frame; the
    'Preview' and 'Edit cover' pills sit on top of it, so only the band between them is compared.
    """
    layout = share.layout()
    sx, sy = image.width / layout.width, image.height / layout.height
    band = (card.left + 6, preview.top + preview.height + 4, card.left + card.width - 6, edit.top - 4)
    shown = image.crop(tuple(round(v * s) for v, s in zip(band, (sx, sy, sx, sy)))).resize((64, 64))
    frame = frame.convert("RGB")
    fw, fh = frame.size
    best = None
    # The card's drawn edge sits a few points off its accessibility frame; search that slack.
    for grow in range(-4, 9, 2):
        for shift in range(-4, 5, 2):
            width, height = card.width + grow, card.height + grow * card.height / card.width
            scale = max(width / fw, height / fh)
            cx, cy = card.x, card.y + shift
            box = (fw / 2 + (band[0] - cx) / scale, fh / 2 + (band[1] - cy) / scale,
                   fw / 2 + (band[2] - cx) / scale, fh / 2 + (band[3] - cy) / scale)
            if box[0] < 0 or box[1] < 0 or box[2] > fw or box[3] > fh:
                continue
            expected = frame.crop(tuple(round(v) for v in box)).resize((64, 64))
            score = sum(ImageStat.Stat(ImageChops.difference(shown, expected)).mean) / 3
            best = score if best is None else min(best, score)
    if best is None:
        raise Stop("Instagram's cover card does not fit the video frame; nothing was posted")
    return best


def prove_cover(data: dict, evidence_dir: Path) -> dict:
    items = wait_for(CAPTION_ID)
    by_label(items, "New reel", "StaticText")
    card = next((e for e in items if e.type == "Other" and e.label == "Edit cover"), None)
    if card is None:
        raise Stop("Instagram's cover card is not on screen; nothing was posted")
    image = screen_image()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    shot = evidence_dir / f"release{data['releaseId']}-instagram-cover-{time.strftime('%Y%m%d-%H%M%S')}.png"
    image.save(shot)
    score = cover_score(image, first_frame(Path(data["sourcePath"])), card,
                        by_label(items, "Preview", "Button"), by_label(items, "Edit cover", "Button"))
    if score > COVER_LIMIT:
        raise Stop(f"Instagram's cover is not the first frame (difference {score:.1f}); nothing was posted")
    return {"difference": round(score, 1), "screenshot": shot.name}


def type_caption(caption: str) -> None:
    field = by_name(elements(), CAPTION_ID)
    if field.value not in ("", "Add a caption..."):
        raise Stop("Instagram's caption already has text; nothing was posted")
    phone.tap(field.left + 40, field.top + 20)
    time.sleep(2)
    if not any(e.type == "Keyboard" for e in elements()):
        raise Stop("Instagram's caption field did not open the keyboard; nothing was posted")
    phone.type_text(caption)
    time.sleep(2.5)
    items = elements()
    if by_name(items, CAPTION_ID).value != caption:
        raise Stop("Instagram's caption does not match the approved text; nothing was posted")
    tap(by_label(items, "OK", "Button"))


def scroll(down: bool) -> None:
    layout = share.layout()
    top, bottom = layout.height * 0.37, layout.height * 0.73
    phone.swipe(layout.width / 2, bottom if down else top, layout.width / 2, top if down else bottom, 0.4)
    time.sleep(1.5)


def crossposts_on(wanted: dict = CROSSPOSTS, off: dict | None = None, *, turn_off: bool = False) -> dict:
    """Post now: Threads and Facebook on for this reel. Never touches 'Stop sharing all reels'.

    Schedule passes Facebook as ``wanted`` and Threads as ``off``: Instagram's scheduler makes
    Threads unavailable, and a Threads crosspost that is somehow on would post it twice.
    """
    scroll(down=True)
    tap(by_label(elements(), "Also share on", "Cell", prefix=True), 2.5)
    states = {}
    for platform, service in (off or {}).items():
        items = elements()
        cell = next((e for e in items if e.type == "Cell" and f", {service} · " in e.label), None)
        if cell is not None and not cell.label.endswith(", Off"):
            if not turn_off:
                raise Stop(f"Instagram would crosspost this scheduled reel to {service}; nothing was posted")
            # Settings turned this crosspost off: off for THIS reel only, never "Stop sharing all reels".
            switch = [e for e in items if e.name == "share-service-cell-switch" and cell.top <= e.y <= cell.top + cell.height]
            if len(switch) != 1:
                raise Stop(f"Expected one {service} crosspost switch; nothing was posted")
            tap(switch[0], 1.8)
            tap(by_label(elements(), "Don't share this reel", "Button"), 1.8)
            cell = next(e for e in elements() if e.type == "Cell" and f", {service} · " in e.label)
            if not cell.label.endswith(", Off"):
                raise Stop(f"The {service} crosspost did not turn off; nothing was posted")
        states[platform] = "off"
    for platform, service in wanted.items():
        items = elements()
        cell = next((e for e in items if e.type == "Cell" and f", {service} · " in e.label), None)
        if cell is None:
            raise Stop(f"Instagram does not offer the {service} crosspost; nothing was posted")
        if cell.label.endswith(", Off"):
            switch = [e for e in items if e.name == "share-service-cell-switch" and cell.top <= e.y <= cell.top + cell.height]
            if len(switch) != 1:
                raise Stop(f"Expected one {service} crosspost switch; nothing was posted")
            tap(switch[0], 1.8)
            prompt = [e for e in elements() if e.type == "Button" and e.label == "Share this reel"]
            if len(prompt) == 1:
                tap(prompt[0], 1.8)
            cell = next(e for e in elements() if e.type == "Cell" and f", {service} · " in e.label)
        if not cell.label.endswith(", On"):
            raise Stop(f"The {service} crosspost is not on; nothing was posted")
        states[platform] = "on"
    tap(by_name(elements(), "BackButton"), 2)
    return states


def not_scheduled() -> None:
    items = elements()
    tap(by_label(items, "More options", "Cell"), 2)
    items = elements()
    schedule = [e for e in items if e.type == "Switch" and "Schedule this reel" in e.label]
    if len(schedule) != 1 or not schedule[0].label.startswith("Not checked"):
        raise Stop("Instagram would schedule this reel, but it is a Post now release; nothing was posted")
    tap(by_name(items, "BackButton"), 2)


# ---- Schedule mode: More options -> "Schedule this reel" -> "Schedule reel" sheet ------------


def _frame(element: Element) -> tuple:
    return element.left, element.top, element.width, element.height


def tap_point(x: float, y: float) -> None:
    phone.tap(x, y)


def sheet_parts(items) -> dict | None:
    """The "Schedule reel" sheet's live frames, or None when it is not the screen shown."""
    titles = [e for e in items if e.type == "StaticText" and e.label == "Schedule reel"]
    done = [e for e in items if e.type == "Button" and e.label == "Done"]
    if len(titles) != 1 or len(done) != 1:
        return None
    notes = [e for e in items if e.type == "StaticText" and e.label.startswith("Time zone is based on")]
    sheets = [e for e in items if e.name == "ig-partial-modal-sheet-view-controller-content"]
    return {"title": _frame(titles[0]), "done": done[0], "note": _frame(notes[0]) if len(notes) == 1 else None,
            "left": sheets[0].left if sheets else 0.0,
            "right": sheets[0].left + sheets[0].width if sheets else share.layout().width}


def wait_items(predicate, what: str, timeout: float | None = None) -> tuple[Element, ...]:
    deadline = time.monotonic() + (SHEET_TIMEOUT if timeout is None else timeout)
    while True:
        items = elements()
        if predicate(items):
            return items
        if time.monotonic() >= deadline:
            visible = [e.label for e in items if e.label][:16]
            raise Stop(f"Instagram did not show {what}; visible: {visible}. Nothing was posted")
        time.sleep(0.6)


def wait_sheet(what: str) -> dict:
    return sheet_parts(wait_items(lambda rows: sheet_parts(rows) is not None, what))


def calendar_open(items) -> bool:
    return (any(e.name == "DatePicker.NextMonth" for e in items)
            and any(e.name == "PopoverDismissRegion" for e in items))


def wheels_open(items) -> bool:
    return any(e.type == "PickerWheel" for e in items)


def close_popover(items, sheet: dict, what: str) -> dict:
    """Close a picker popover by tapping the sheet's own header text beside it, then prove the sheet."""
    frames = [_frame(e) for e in items if e.type in ("DatePicker", "Picker", "PickerWheel")]
    if not frames:
        raise Stop(f"Instagram's {what} has no frame to avoid; nothing was posted")
    left, top = min(f[0] for f in frames), min(f[1] for f in frames)
    right, bottom = max(f[0] + f[2] for f in frames), max(f[1] + f[3] for f in frames)
    popover = (left, top, right - left, bottom - top)
    try:
        point = ns.outside_point(ns.sheet_header_candidates(sheet["title"], sheet["note"], popover,
                                                            sheet["left"], sheet["right"]), [popover])
    except ValueError as exc:
        raise Stop(f"{exc}; nothing was posted") from exc
    tap_point(*point)
    # A tap that also closed the sheet turns scheduling off (recording schedule-time): stop then.
    return wait_sheet(f"its schedule sheet after closing the {what} (scheduling may have turned off)")


def open_row(sheet: dict, row: str, opened, what: str, timeout: float | None = None) -> tuple[Element, ...]:
    """Tap a Date/Time row placed from the live Done button; the popover appearing proves the tap."""
    try:
        points = ns.instagram_row_points(_frame(sheet["done"]), sheet["title"], sheet["note"])
    except ValueError as exc:
        raise Stop(f"{exc}; nothing was posted") from exc
    tap_point(*points[row])
    return wait_items(opened, what, timeout)


def schedule_date(sheet: dict, moment: datetime, now: datetime) -> dict:
    items = open_row(sheet, "date", calendar_open, "its calendar (the Date row tap did not land)")
    today = ns.phone_today((e.label for e in items), int(ns.shown_month(items).split()[-1]))
    if today is not None and today != now.astimezone(moment.tzinfo).date():
        raise Stop("The iPhone's date differs from the app's time zone; set both to the same zone. "
                   "Nothing was posted")
    try:
        items = ns.pick_day(elements, tap_point, moment.date(), pause=time.sleep)
    except ValueError as exc:
        raise Stop(str(exc)) from exc
    sheet = (close_popover(items, sheet, "calendar") if calendar_open(items)
             else wait_sheet("its schedule sheet after the date"))
    # Read back: the row is not in the tree, so re-open the calendar and check the selection.
    items = open_row(sheet, "date", calendar_open, "its calendar again")
    if not ns.day_selected(items, moment.date()):
        raise Stop(f"Instagram did not keep {ns.day_button_label(moment.date())}; nothing was posted")
    return close_popover(items, sheet, "calendar")


def schedule_time(sheet: dict, moment: datetime) -> dict:
    # Never recorded: only the iOS hour/minute/AM-PM wheels are trusted; anything else stops here.
    try:
        items = open_row(sheet, "time", wheels_open, "time wheels", timeout=TIME_WHEELS_TIMEOUT)
        ns.classify_wheels(ns.wheel_values(items))
    except (Stop, ValueError) as exc:
        raise Stop(f"Instagram's time picker is not mapped yet ({exc})") from exc
    try:
        ns.set_wheels(elements, tap_point, moment, pause=time.sleep)
    except ValueError as exc:
        raise Stop(f"Instagram's time wheels did not take {ns.picker_time_label(moment)}: {exc}") from exc
    sheet = close_popover(elements(), sheet, "time wheels")
    items = open_row(sheet, "time", wheels_open, "its time wheels again", timeout=TIME_WHEELS_TIMEOUT)
    if not ns.wheels_show(items, moment):
        raise Stop(f"Instagram did not keep {ns.picker_time_label(moment)}; nothing was posted")
    return close_popover(items, sheet, "time wheels")


def schedule_row(items) -> Element:
    rows = [e for e in items if e.type == "Switch" and "Schedule this reel" in e.label]
    if len(rows) != 1:
        raise Stop("Instagram's Schedule this reel switch is missing or ambiguous; nothing was posted")
    return rows[0]


def more_options_open(items) -> bool:
    return any(e.type == "Switch" and "Schedule this reel" in e.label for e in items)


def schedule_reel(moment: datetime, now: datetime) -> dict:
    """Turn on "Schedule this reel", enter the slot in the phone's zone, prove it, back to the composer."""
    tap(by_label(elements(), "More options", "Cell"), 2)
    items = wait_items(more_options_open, "More options")
    row = schedule_row(items)
    if not row.label.startswith("Not checked"):
        raise Stop("Instagram's schedule was already on with an unknown time; nothing was posted")
    switch = [e for e in items if e.name == "igds-switch" and row.top <= e.y <= row.top + row.height]
    if len(switch) != 1:
        raise Stop("Expected one Schedule this reel switch; nothing was posted")
    tap(switch[0], 1.5)
    sheet = wait_sheet("its Schedule reel sheet")
    share.stage("instagram_schedule_date")
    sheet = schedule_date(sheet, moment, now)
    share.stage("instagram_schedule_time")
    sheet = schedule_time(sheet, moment)
    tap(sheet["done"], 1.5)
    items = wait_items(more_options_open, "More options after Done")
    if not schedule_row(items).label.startswith("Checked"):
        raise Stop("Instagram's Schedule this reel switch is off after Done; nothing was posted")
    tap(by_name(items, "BackButton"), 2)
    return {"date": ns.day_button_label(moment.date()), "time": ns.picker_time_label(moment)}


def composer_ready(caption: str, *, profiles: str = "2 profiles", button: str = "Share") -> Element:
    scroll(down=False)
    items = elements()
    by_label(items, "New reel", "StaticText")
    if by_name(items, CAPTION_ID).value != caption:
        raise Stop("Instagram's caption changed; nothing was posted")
    scroll(down=True)
    items = elements()
    summary = by_label(items, "Also share on", "Cell", prefix=True).label
    if profiles not in summary:
        raise Stop(f"Instagram's crosspost summary is {summary!r}, not {profiles}; nothing was posted")
    post = by_name(items, SHARE_ID, "Button")
    if post.label != button:
        raise Stop(f"Instagram's final button reads {post.label!r}, not {button}; nothing was posted")
    return post


# ---- run ---------------------------------------------------------------------


def leave_to_home() -> None:
    try:
        from video_drop.phone import device
        release_frozen_app(device.ios_path())
    except Exception:
        pass


def schedule_and_submit(store: Store, data: dict, cover: dict, *, commit: bool, clock) -> dict:
    """Schedule mode after the caption: slot, Facebook on / Threads off, then one Schedule tap."""
    release_id, moment = data["releaseId"], data["moment"]
    shown = schedule_reel(moment, clock())
    crossposts = crossposts_on(SCHEDULE_CROSSPOSTS, off={"threads": "Threads"})
    post = composer_ready(data["caption"], profiles="1 profile", button="Schedule")
    result = {"platform": "instagram", "releaseId": release_id, "cover": cover, "crossposts": crossposts,
              "slot": moment.isoformat(), "shown": shown}
    if not commit:
        return {"kind": "ready", **result}
    if moment - ns.MIN_LEAD <= clock().astimezone(moment.tzinfo):
        raise Stop("The slot is now too close to schedule; reserve a later one. Nothing was posted")
    # Record uncertainty before the one final tap. Core claims Facebook with Instagram in Schedule.
    store.mark_unconfirmed(release_id, "instagram", expected_revision=data["revisionHash"])
    facebook = next(d for d in store.release(release_id)["destinations"] if d["platform"] == "facebook")
    if facebook["status"] != "unconfirmed":
        raise Stop("Facebook was not claimed with Instagram's scheduled upload; Schedule was NOT tapped, but "
                   "Instagram is marked unconfirmed. Clear it after checking the account")
    with share.busy("Instagram upload", 30, linger=share.upload_linger(data["sizeBytes"])):
        phone.tap(post.x, post.y)
        time.sleep(20)  # the upload still runs before the reel waits for its slot
        leave_to_home()  # its feed autoplays video; read nothing there
    # TODO(receipt): open Profile > Scheduled content, read its rows and screenshot, and pass them to
    # store.record_observed_schedule (instagram_schedule.verified_scheduled_reel checks caption, time
    # and first-frame cover). That screen is not recorded yet, so the result stays unconfirmed.
    return {"kind": "unconfirmed", **result,
            "message": "Schedule tapped; check Instagram's Scheduled content (and Facebook) for this reel at "
                       f"{shown['date']} {shown['time']} before any retry"}


@phone_lock.locked("Instagram via Edits")
def run(release_id: int, db: Path, *, commit: bool = False, now=None) -> dict:
    if commit and os.environ.get("VIDEO_DROP_TEST_MODE") == "1":
        raise share.PhoneUploadError("Posting is disabled in this test session")
    clock = now or (lambda: datetime.now(timezone.utc))
    share.set_source_db(db)
    with Store(db, load_targets(db.parent)) as store:
        data = release_input(store, release_id, clock())
        share.connect_sidetap()
        global phone
        phone = share.phone
        preflight.phone = phone
        phone.unlock()
        with share.busy("Instagram via Edits", 1800), run_guards(phone, store.phone_checks()):
            try:
                preflight.ensure_instagram_account(data["account"])
            except ValueError as exc:
                raise Stop(f"{exc}. Nothing was posted") from exc
            # The profile is the last screen showing the post count before the upload; the
            # receipt later requires exactly one more post plus the first-frame tile.
            try:
                receipts.save_baseline(db.parent, release_id, "instagram", receipts.instagram_profile(elements()))
            except Exception as exc:  # the receipt then fails closed; posting is not blocked
                share.stage(f"instagram_baseline_unread: {type(exc).__name__}")
            if commit and store.phone_checks()["removeAfterPost"]:
                # The project this upload creates in Edits is the one cleanup may trash later.
                try:
                    from scripts import phone_cleanup
                    phone_cleanup.note_projects(db.parent, release_id)
                except Exception as exc:  # cleanup then skips this release; posting is not blocked
                    share.stage(f"edits_projects_unread: {type(exc).__name__}")
            leave_to_home()
            share.layout(refresh=True)
            share.open_source_file(data)
            share.choose_share_app("Edits", expected_bundle=EDITS_BUNDLE)
            edits_export(data)
            cover = prove_cover(data, db.parent / "evidence")
            type_caption(data["caption"])
            if data["mode"] == "schedule":
                return schedule_and_submit(store, data, cover, commit=commit, clock=clock)
            crossposts = crossposts_on({p: CROSSPOSTS[p] for p in data["crossposts"]},
                                       {p: s for p, s in CROSSPOSTS.items() if p not in data["crossposts"]},
                                       turn_off=True)
            not_scheduled()
            count = len(data["crossposts"])
            post = composer_ready(data["caption"], profiles=f"{count} profile{'s' if count != 1 else ''}" if count else "")
            if not commit:
                # A dry run leaves no composer behind: a later run would reopen on it (2026-10-02).
                preflight.discard_composer(elements())
                return {"kind": "ready", "platform": "instagram", "releaseId": release_id, "cover": cover,
                        "crossposts": crossposts}
            # Record uncertainty before the one final tap: a timeout after it can mean a live post.
            # In Post now, core claims Facebook and Threads with Instagram in one transaction.
            store.mark_unconfirmed(release_id, "instagram", expected_revision=data["revisionHash"])
            with share.busy("Instagram upload", 30, linger=share.upload_linger(data["sizeBytes"])):
                phone.tap(post.x, post.y)
                time.sleep(20)  # "Step 2 of 3: Processing — You can close Instagram"
                leave_to_home()  # its feed autoplays video; read nothing there
            return {"kind": "unconfirmed", "platform": "instagram", "releaseId": release_id, "cover": cover,
                    "crossposts": crossposts,
                    "message": "Share tapped; check Instagram, Facebook and Threads for the post before any retry"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_id", type=int)
    parser.add_argument("--db", type=Path, default=ROOT / ".state" / "video-drop.sqlite")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.release_id, args.db, commit=args.commit)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"kind": "error", "message": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from None
