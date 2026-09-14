import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from event_feed import (  # noqa: E402
    Event,
    OSLO,
    deduplicate,
    notification_window,
    overlaps,
    parse_norwegian_date_range,
    parse_rindal_teaser,
    write_feed,
)


def event(title, start, end, location="Sted"):
    return Event(
        source="Test",
        municipality="Orkland",
        source_id=title,
        title=title,
        start=start,
        end=end,
        location=location,
        url="https://example.com/event",
    )


class WindowTests(unittest.TestCase):
    def test_daily_is_weekdays_only_and_starts_at_1530(self):
        monday = datetime(2026, 9, 14, 15, 0, tzinfo=OSLO)
        start, end = notification_window("daily", monday)
        self.assertEqual((start.hour, start.minute), (15, 30))
        self.assertEqual((end.hour, end.minute), (23, 59))

        saturday = datetime(2026, 9, 19, 15, 0, tzinfo=OSLO)
        with self.assertRaises(ValueError):
            notification_window("daily", saturday)

    def test_weekend_from_friday(self):
        friday = datetime(2026, 9, 18, 8, 30, tzinfo=OSLO)
        start, end = notification_window("weekend", friday)
        self.assertEqual(start.date(), date(2026, 9, 19))
        self.assertEqual(end.date(), date(2026, 9, 20))

    def test_ongoing_event_is_included(self):
        window_start = datetime(2026, 9, 14, 15, 30, tzinfo=OSLO)
        window_end = datetime(2026, 9, 14, 23, 59, tzinfo=OSLO)
        ongoing = event(
            "Pågår",
            datetime(2026, 9, 14, 14, 0, tzinfo=OSLO),
            datetime(2026, 9, 14, 16, 0, tzinfo=OSLO),
        )
        ended = event(
            "Ferdig",
            datetime(2026, 9, 14, 12, 0, tzinfo=OSLO),
            datetime(2026, 9, 14, 15, 29, tzinfo=OSLO),
        )
        self.assertTrue(overlaps(ongoing, window_start, window_end))
        self.assertFalse(overlaps(ended, window_start, window_end))


class ParserTests(unittest.TestCase):
    def test_norwegian_date_range(self):
        self.assertEqual(
            parse_norwegian_date_range(
                "Mandag 14. september 2026 - søndag 20. september 2026"
            ),
            (date(2026, 9, 14), date(2026, 9, 20)),
        )

    def test_rindal_teaser(self):
        content = """
        <span class="cc-teaser-title-text">Høstmarked</span>
        <div class="cc-teaser-meta-item--date">
          <div class="cc-teaser-meta-item-value-content">Lørdag 19. september 2026</div>
        </div>
        <div class="cc-teaser-meta-item--time">
          <div class="cc-teaser-meta-item-value-content">kl. 11:00 - 16:00</div>
        </div>
        <div class="cc-teaser-meta-item--location">
          <div class="cc-teaser-meta-item-value-content">Rindal sentrum</div>
        </div>
        """
        parsed = parse_rindal_teaser(
            {
                "content": content,
                "navigateUrl": "/Kalender/CalendarEvent.aspx?Id=42&MId1=1333",
            }
        )
        self.assertEqual(parsed.title, "Høstmarked")
        self.assertEqual(parsed.start.hour, 11)
        self.assertEqual(parsed.end.hour, 16)
        self.assertEqual(parsed.location, "Rindal sentrum")

    def test_deduplicate_exact_title_time_and_place(self):
        start = datetime(2026, 9, 14, 18, 0, tzinfo=OSLO)
        first = event("Konsert!", start, start, "Kulturhuset")
        second = event("Konsert", start, start, "Kulturhuset")
        self.assertEqual(len(deduplicate([first, second])), 1)


class FeedTests(unittest.TestCase):
    def test_feed_is_valid_and_same_digest_is_not_repeated(self):
        now = datetime(2026, 9, 14, 15, 0, tzinfo=OSLO)
        events = [
            event(
                "Konsert",
                datetime(2026, 9, 14, 18, 0, tzinfo=OSLO),
                datetime(2026, 9, 14, 20, 0, tzinfo=OSLO),
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.xml"
            history = Path(directory) / "history.json"
            self.assertTrue(
                write_feed(
                    mode="daily",
                    now=now,
                    events=events,
                    output_path=output,
                    history_path=history,
                    feed_url="https://example.com/feed.xml",
                )
            )
            self.assertFalse(
                write_feed(
                    mode="daily",
                    now=now,
                    events=events,
                    output_path=output,
                    history_path=history,
                    feed_url="https://example.com/feed.xml",
                )
            )
            root = ET.parse(output).getroot()
            self.assertEqual(len(root.findall("./channel/item")), 1)
            self.assertEqual(len(json.loads(history.read_text())), 1)


if __name__ == "__main__":
    unittest.main()

