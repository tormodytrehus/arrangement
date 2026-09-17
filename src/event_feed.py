#!/usr/bin/env python3
"""Lag en samlet RSS-feed fra fire lokale arrangementskalendere."""

from __future__ import annotations

import argparse
import hashlib
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


OSLO = ZoneInfo("Europe/Oslo")
USER_AGENT = "orkland-region-rss/1.0 (+GitHub Actions)"
RINDAL_IDENTIFIER = "7rGnwIURvIGmumPyzNw71YnLIB9EijEDm3Rei68C4iRuG3un5AlhNaP5P_F8rDKL"
MUNICIPALITY_ORDER = {"Orkland": 0, "Skaun": 1, "Heim": 2, "Rindal": 3}

NORWEGIAN_MONTHS = {
    "januar": 1,
    "februar": 2,
    "mars": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "desember": 12,
}

NORWEGIAN_WEEKDAYS = [
    "mandag",
    "tirsdag",
    "onsdag",
    "torsdag",
    "fredag",
    "lørdag",
    "søndag",
]

NORWEGIAN_WEEKDAYS_SHORT = [
    "man",
    "tir",
    "ons",
    "tor",
    "fre",
    "lør",
    "søn",
]


@dataclass(frozen=True)
class Event:
    source: str
    municipality: str
    source_id: str
    title: str
    start: datetime
    end: datetime
    location: str
    url: str


class SourceError(RuntimeError):
    pass


def request_bytes(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    attempts: int = 3,
) -> bytes:
    merged_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    request = urllib.request.Request(url, data=data, headers=merged_headers)
    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            response = (
                opener.open(request, timeout=30)
                if opener is not None
                else urllib.request.urlopen(request, timeout=30)
            )
            with response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))

    raise SourceError(f"Kunne ikke hente {url}: {last_error}")


def request_json(url: str, **kwargs: object) -> object:
    try:
        return json.loads(request_bytes(url, **kwargs).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError(f"Ugyldig JSON fra {url}: {exc}") from exc


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace(" ", "T"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=OSLO)
    return parsed.astimezone(OSLO)


def combine_local(
    day: date,
    value: str | None,
    *,
    end_of_day: bool = False,
) -> datetime:
    if value:
        match = re.search(r"(\d{1,2}):(\d{2})", value)
        if match:
            clock = datetime_time(int(match.group(1)), int(match.group(2)))
            return datetime.combine(day, clock, OSLO)

    clock = datetime_time(23, 59, 59) if end_of_day else datetime_time.min
    return datetime.combine(day, clock, OSLO)


def fetch_orkland(start_day: date, end_day: date) -> list[Event]:
    start_bound = combine_local(start_day, None)
    end_bound = combine_local(end_day, None, end_of_day=True)

    graph_filter = (
        '{fromDate:"%s",untilDate:"%s",groupRepetitionsByDay:true}'
        % (
            start_bound.strftime("%Y-%m-%d %H:%M:%S%z"),
            end_bound.strftime("%Y-%m-%d %H:%M:%S%z"),
        )
    )

    query = """{
      events(filter: FILTER, page: 0, pageSize: 250) {
        data {
          id title_nb startDate endDate startTime event_slug eventLink
          eventCancelled venue { name address }
        }
        totalCount
      }
    }""".replace("FILTER", graph_filter)

    endpoint = "https://visit-orkland.web.app/graphQL"

    payload = request_json(
        endpoint,
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    if not isinstance(payload, dict) or payload.get("errors"):
        raise SourceError(f"GraphQL-feil fra Visit Orkland: {payload!r}")

    try:
        raw_events = payload["data"]["events"]["data"]
    except (KeyError, TypeError) as exc:
        raise SourceError("Uventet svarformat fra Visit Orkland") from exc

    events: list[Event] = []

    for raw in raw_events:
        if raw.get("eventCancelled") or not raw.get("startDate"):
            continue

        start = parse_iso_datetime(raw["startDate"])
        end = parse_iso_datetime(raw["endDate"]) if raw.get("endDate") else start
        venue = raw.get("venue") or {}
        slug = raw.get("event_slug") or str(raw.get("id", ""))

        url = (
            raw.get("eventLink")
            or f"https://www.visitorkland.no/arrangementer/{slug}"
        )

        events.append(
            Event(
                source="Visit Orkland",
                municipality="Orkland",
                source_id=f"orkland:{raw.get('id', slug)}:{start.isoformat()}",
                title=(raw.get("title_nb") or "Uten tittel").strip(),
                start=start,
                end=max(end, start),
                location=(
                    venue.get("name")
                    or venue.get("address")
                    or ""
                ).strip(),
                url=url,
            )
        )

    return events


def fetch_foreningsportal(
    host: str,
    municipality: str,
    start_day: date,
    end_day: date,
) -> list[Event]:
    params = urllib.parse.urlencode(
        {
            "type": "items",
            "datefrom": start_day.strftime("%d.%m.%Y"),
            "dateto": end_day.strftime("%d.%m.%Y"),
        }
    )

    url = f"{host}/wwdok/37199-0.html?{params}"
    payload = request_json(url)

    if not isinstance(payload, list):
        raise SourceError(f"Uventet svarformat fra {host}")

    events: list[Event] = []

    for raw in payload:
        if not isinstance(raw, dict) or not raw.get("startDate"):
            continue

        try:
            first_day = date.fromisoformat(raw["startDate"])
            last_day = (
                date.fromisoformat(raw["endDate"])
                if raw.get("endDate")
                else first_day
            )
        except ValueError:
            continue

        start = combine_local(first_day, raw.get("startTime"))

        if raw.get("endTime"):
            end = combine_local(last_day, raw.get("endTime"))
        elif raw.get("endDate"):
            end = combine_local(last_day, None, end_of_day=True)
        elif not raw.get("startTime"):
            end = combine_local(first_day, None, end_of_day=True)
        else:
            end = start

        source_id = str(
            raw.get("id")
            or raw.get("urlText")
            or raw.get("title", "")
        )

        events.append(
            Event(
                source=(
                    "MittSkaun"
                    if municipality == "Skaun"
                    else "iHeim"
                ),
                municipality=municipality,
                source_id=(
                    f"{municipality.lower()}:"
                    f"{source_id}:{start.isoformat()}"
                ),
                title=(raw.get("title") or "Uten tittel").strip(),
                start=start,
                end=max(end, start),
                location=(raw.get("location") or "").strip(),
                url=(
                    f"{host}/hendelse?"
                    f"Id={urllib.parse.quote(source_id)}"
                ),
            )
        )

    return events


def strip_markup(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    decoded = html.unescape(without_tags)
    return re.sub(r"\s+", " ", decoded).strip()


def teaser_value(content: str, modifier: str) -> str:
    match = re.search(
        rf"cc-teaser-meta-item--{re.escape(modifier)}.*?"
        r"cc-teaser-meta-item-value-content[^>]*>(.*?)</div>",
        content,
        re.IGNORECASE | re.DOTALL,
    )

    return strip_markup(match.group(1)) if match else ""


def parse_norwegian_date_range(value: str) -> tuple[date, date]:
    matches = re.findall(
        r"(\d{1,2})\.\s*([a-zæøå]+)\s+(\d{4})",
        value.lower(),
        re.IGNORECASE,
    )

    if not matches:
        raise ValueError(f"Fant ingen dato i {value!r}")

    def convert(parts: tuple[str, str, str]) -> date:
        day, month_name, year = parts
        return date(
            int(year),
            NORWEGIAN_MONTHS[month_name],
            int(day),
        )

    return convert(matches[0]), convert(matches[-1])


def parse_rindal_teaser(item: dict[str, object]) -> Event:
    content = str(item.get("content") or "")

    title_match = re.search(
        r"cc-teaser-title-text[^>]*>(.*?)</span>",
        content,
        re.IGNORECASE | re.DOTALL,
    )

    title = (
        strip_markup(title_match.group(1))
        if title_match
        else "Uten tittel"
    )

    first_day, last_day = parse_norwegian_date_range(
        teaser_value(content, "date")
    )

    times = re.findall(
        r"(\d{1,2}):(\d{2})",
        teaser_value(content, "time"),
    )

    if times:
        start = datetime.combine(
            first_day,
            datetime_time(
                int(times[0][0]),
                int(times[0][1]),
            ),
            OSLO,
        )

        end_parts = times[-1]

        end = datetime.combine(
            last_day,
            datetime_time(
                int(end_parts[0]),
                int(end_parts[1]),
            ),
            OSLO,
        )
    else:
        start = combine_local(first_day, None)
        end = combine_local(
            last_day,
            None,
            end_of_day=True,
        )

    relative_url = str(item.get("navigateUrl") or "")
    absolute_url = urllib.parse.urljoin(
        "https://www.kulturboksen.no/",
        relative_url,
    )

    id_match = re.search(
        r"[?&]Id=(\d+)",
        absolute_url,
        re.IGNORECASE,
    )

    source_id = (
        id_match.group(1)
        if id_match
        else hashlib.sha256(
            absolute_url.encode()
        ).hexdigest()[:16]
    )

    return Event(
        source="Kulturboksen",
        municipality="Rindal",
        source_id=f"rindal:{source_id}:{start.isoformat()}",
        title=title,
        start=start,
        end=max(end, start),
        location=teaser_value(content, "location"),
        url=absolute_url,
    )


def fetch_rindal(mode: str) -> list[Event]:
    cookie_jar = http.cookiejar.CookieJar()

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )

    home = "https://www.kulturboksen.no/"
    request_bytes(home, opener=opener)

    date_filter = "today" if mode == "daily" else "weekend"

    endpoint = (
        "https://www.kulturboksen.no/api/presentation/v2/"
        f"filtervisning/{RINDAL_IDENTIFIER}/init?"
        f"{urllib.parse.urlencode({'date': date_filter})}"
    )

    payload = request_json(
        endpoint,
        opener=opener,
        headers={"Referer": home},
    )

    try:
        body = payload["content"]["body"]
        items = body["result"]["items"]
        total = int(
            body["result"]["paginationComponent"]["totalItemCount"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError(
            "Uventet svarformat fra Kulturboksen"
        ) from exc

    if total > len(items):
        raise SourceError(
            f"Kulturboksen returnerte bare {len(items)} "
            f"av {total} treff; adapteren må oppdateres "
            "for paginering"
        )

    events: list[Event] = []

    for item in items:
        try:
            events.append(parse_rindal_teaser(item))
        except (KeyError, ValueError) as exc:
            raise SourceError(
                f"Kunne ikke tolke et Kulturboksen-treff: {exc}"
            ) from exc

    return events


def notification_window(
    mode: str,
    now: datetime,
) -> tuple[datetime, datetime]:
    local_now = now.astimezone(OSLO)
    weekday = local_now.weekday()

    if mode == "daily":
        if weekday > 4:
            raise ValueError(
                "Dagsvarsel skal bare kjøres mandag–fredag"
            )

        day = local_now.date()

        return (
            datetime.combine(
                day,
                datetime_time.min,
                OSLO,
            ),
            datetime.combine(
                day,
                datetime_time(23, 59, 59),
                OSLO,
            ),
        )

    if mode == "friday":
        if weekday != 4:
            raise ValueError(
                "Fredagsvarsel skal bare kjøres på fredag"
            )

        friday = local_now.date()
        sunday = friday + timedelta(days=2)

        return (
            datetime.combine(
                friday,
                datetime_time.min,
                OSLO,
            ),
            datetime.combine(
                sunday,
                datetime_time(23, 59, 59),
                OSLO,
            ),
        )

    if mode == "weekend":
        days_until_saturday = (
            5 - weekday
        ) % 7

        saturday = (
            local_now.date()
            + timedelta(days=days_until_saturday)
        )

        sunday = saturday + timedelta(days=1)

        return (
            combine_local(saturday, None),
            combine_local(
                sunday,
                None,
                end_of_day=True,
            ),
        )

    raise ValueError(f"Ukjent modus: {mode}")


def overlaps(
    event: Event,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    return (
        event.start <= window_end
        and event.end >= window_start
    )


def normalize_text(value: str) -> str:
    value = unicodedata.normalize(
        "NFKD",
        value.casefold(),
    )

    return re.sub(r"[^a-z0-9]+", "", value)


def deduplicate(events: Iterable[Event]) -> list[Event]:
    unique: dict[tuple[str, str, str], Event] = {}

    for event in events:
        key = (
            normalize_text(event.title),
            event.start.isoformat(),
            normalize_text(event.location),
        )

        unique.setdefault(key, event)

    return sorted(
        unique.values(),
        key=lambda event: (
            event.start,
            MUNICIPALITY_ORDER.get(
                event.municipality,
                99,
            ),
            event.title.casefold(),
        ),
    )


def fetch_all(
    mode: str,
    window_start: datetime,
    window_end: datetime,
) -> list[Event]:
    first_day = window_start.date()
    last_day = window_end.date()

    sources = [
        (
            "Visit Orkland",
            lambda: fetch_orkland(
                first_day,
                last_day,
            ),
        ),
        (
            "MittSkaun",
            lambda: fetch_foreningsportal(
                "https://mittskaun.no",
                "Skaun",
                first_day,
                last_day,
            ),
        ),
        (
            "iHeim",
            lambda: fetch_foreningsportal(
                "https://iheim.no",
                "Heim",
                first_day,
                last_day,
            ),
        ),
        (
            "Kulturboksen",
            lambda: (
                fetch_rindal("daily")
                + fetch_rindal("weekend")
                if mode == "friday"
                else fetch_rindal(mode)
            ),
        ),
    ]

    all_events: list[Event] = []
    failures: list[str] = []

    for name, fetcher in sources:
        try:
            all_events.extend(fetcher())
        except Exception as exc:
            failures.append(f"{name}: {exc}")

    if failures:
        raise SourceError(
            "Én eller flere kilder feilet:\n- "
            + "\n- ".join(failures)
        )

    return deduplicate(
        event
        for event in all_events
        if overlaps(
            event,
            window_start,
            window_end,
        )
    )


def norwegian_date(
    day: date,
    *,
    include_weekday: bool = True,
) -> str:
    prefix = (
        f"{NORWEGIAN_WEEKDAYS[day.weekday()]} "
        if include_weekday
        else ""
    )

    month = list(NORWEGIAN_MONTHS)[day.month - 1]

    return f"{prefix}{day.day}. {month}"


def item_title(
    mode: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    if mode == "daily":
        return (
            "Dagens arrangementer – "
            f"{norwegian_date(window_start.date())}"
        )

    first = window_start.date()
    last = window_end.date()

    if mode == "friday":
        if first.month == last.month:
            month = list(NORWEGIAN_MONTHS)[
                first.month - 1
            ]

            return (
                f"Fredag–søndag {first.day}.–"
                f"{last.day}. {month}"
            )

        return (
            "Fredag–søndag "
            f"{norwegian_date(first, include_weekday=False)}–"
            f"{norwegian_date(last, include_weekday=False)}"
        )

    if first.month == last.month:
        month = list(NORWEGIAN_MONTHS)[
            first.month - 1
        ]

        return (
            f"Helgen {first.day}.–"
            f"{last.day}. {month}"
        )

    return (
        "Helgen "
        f"{norwegian_date(first, include_weekday=False)}–"
        f"{norwegian_date(last, include_weekday=False)}"
    )


def norwegian_short_datetime(value: datetime) -> str:
    weekday = NORWEGIAN_WEEKDAYS_SHORT[
        value.weekday()
    ]

    return f"{weekday} {value:%d.%m %H:%M}"


def clock_range(event: Event) -> str:
    if event.start.date() == event.end.date():
        if (
            event.start.time() == datetime_time.min
            and event.end.time()
            >= datetime_time(23, 59)
        ):
            return "Hele dagen"

        if event.start == event.end:
            return event.start.strftime("%H:%M")

        return (
            f"{event.start:%H:%M}–"
            f"{event.end:%H:%M}"
        )

    return (
        f"{norwegian_short_datetime(event.start)}–"
        f"{norwegian_short_datetime(event.end)}"
    )


def description_html(events: list[Event]) -> str:
    parts: list[str] = []

    municipalities = sorted(
        {event.municipality for event in events},
        key=lambda value: MUNICIPALITY_ORDER.get(
            value,
            99,
        ),
    )

    for municipality in municipalities:
        parts.append(
            f"<p><strong>[{html.escape(municipality)}]"
            "</strong><br>"
        )

        municipality_events = (
            event
            for event in events
            if event.municipality == municipality
        )

        for event in municipality_events:
            location = (
                f" – {html.escape(event.location)}"
                if event.location
                else ""
            )

            parts.append(
                "• "
                f"<strong>{html.escape(clock_range(event))}</strong>: "
                f'<a href="{html.escape(event.url, quote=True)}">'
                f"{html.escape(event.title)}</a>"
                f"{location}<br>"
            )

        parts.append("</p>")

    return "".join(parts)


def load_history(
    path: Path,
) -> list[dict[str, object]]:
    if not path.exists():
        return []

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Kunne ikke lese historikk {path}: {exc}"
        ) from exc

    if not isinstance(payload, list):
        raise RuntimeError(
            f"Historikken i {path} er ikke en liste"
        )

    return payload


def default_feed_url() -> str:
    explicit = os.environ.get("FEED_URL")

    if explicit:
        return explicit

    repository = os.environ.get(
        "GITHUB_REPOSITORY",
        "",
    )

    if "/" in repository:
        owner, name = repository.split("/", 1)

        return (
            f"https://{owner}.github.io/"
            f"{name}/feed.xml"
        )

    return "https://example.invalid/feed.xml"


def render_feed(
    history: list[dict[str, object]],
    feed_url: str,
) -> bytes:
    ET.register_namespace(
        "atom",
        "http://www.w3.org/2005/Atom",
    )

    rss = ET.Element(
        "rss",
        {"version": "2.0"},
    )

    channel = ET.SubElement(rss, "channel")

    ET.SubElement(
        channel,
        "title",
    ).text = (
        "Arrangementer i Orkland, Skaun, "
        "Heim og Rindal"
    )

    ET.SubElement(
        channel,
        "link",
    ).text = feed_url

    ET.SubElement(
        channel,
        "description",
    ).text = (
        "Hverdags- og helgevarsler fra "
        "lokale arrangementskalendere."
    )

    ET.SubElement(
        channel,
        "language",
    ).text = "nb-NO"

    ET.SubElement(
        channel,
        "{http://www.w3.org/2005/Atom}link",
        {
            "href": feed_url,
            "rel": "self",
            "type": "application/rss+xml",
        },
    )

    for entry in history:
        item = ET.SubElement(channel, "item")

        ET.SubElement(
            item,
            "title",
        ).text = str(entry["title"])

        item_guid = str(entry["guid"])

        item_link = (
            f"{feed_url}#item-"
            f"{urllib.parse.quote(item_guid, safe='')}"
        )

        ET.SubElement(
            item,
            "link",
        ).text = item_link

        ET.SubElement(
            item,
            "description",
        ).text = str(entry["description"])

        ET.SubElement(
            item,
            "guid",
            {"isPermaLink": "false"},
        ).text = item_guid

        ET.SubElement(
            item,
            "pubDate",
        ).text = str(entry["pubDate"])

    ET.indent(rss, space="  ")

    return ET.tostring(
        rss,
        encoding="utf-8",
        xml_declaration=True,
    )


def write_feed(
    *,
    mode: str,
    now: datetime,
    events: list[Event],
    output_path: Path,
    history_path: Path,
    feed_url: str,
) -> bool:
    if not events:
        return False

    window_start, window_end = notification_window(
        mode,
        now,
    )

    guid = (
        f"orkland-region-rss:{mode}:"
        f"{window_start.date()}:"
        f"{window_end.date()}"
    )

    history = load_history(history_path)

    if any(
        entry.get("guid") == guid
        for entry in history
    ):
        rendered = render_feed(
            history,
            feed_url,
        )

        if (
            output_path.exists()
            and output_path.read_bytes() == rendered
        ):
            return False

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        output_path.write_bytes(rendered)

        return True

    entry = {
        "guid": guid,
        "title": item_title(
            mode,
            window_start,
            window_end,
        ),
        "description": description_html(events),
        "pubDate": format_datetime(
            now.astimezone(timezone.utc)
        ),
    }

    history.insert(0, entry)
    history = history[:60]

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    history_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    history_path.write_text(
        json.dumps(
            history,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    output_path.write_bytes(
        render_feed(
            history,
            feed_url,
        )
    )

    return True


def check_sources(now: datetime) -> None:
    day = now.astimezone(OSLO).date()

    checks = [
        (
            "Visit Orkland",
            lambda: fetch_orkland(
                day,
                day + timedelta(days=7),
            ),
        ),
        (
            "MittSkaun",
            lambda: fetch_foreningsportal(
                "https://mittskaun.no",
                "Skaun",
                day,
                day + timedelta(days=7),
            ),
        ),
        (
            "iHeim",
            lambda: fetch_foreningsportal(
                "https://iheim.no",
                "Heim",
                day,
                day + timedelta(days=7),
            ),
        ),
        (
            "Kulturboksen",
            lambda: fetch_rindal("daily"),
        ),
    ]

    failed = False

    for name, fetcher in checks:
        try:
            events = fetcher()

            print(
                f"OK  {name}: "
                f"{len(events)} treff"
            )
        except Exception as exc:
            failed = True

            print(
                f"FEIL {name}: {exc}",
                file=sys.stderr,
            )

    if failed:
        raise SystemExit(1)


def parse_args(
    argv: list[str] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
    )

    parser.add_argument(
        "--mode",
        choices=(
            "daily",
            "friday",
            "weekend",
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/feed.xml"),
    )

    parser.add_argument(
        "--history",
        type=Path,
        default=Path("docs/history.json"),
    )

    parser.add_argument(
        "--check-sources",
        action="store_true",
    )

    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
) -> int:
    args = parse_args(argv)
    now = datetime.now(OSLO)

    if args.check_sources:
        check_sources(now)
        return 0

    if not args.mode:
        raise SystemExit(
            "--mode er påkrevd når "
            "--check-sources ikke brukes"
        )

    window_start, window_end = notification_window(
        args.mode,
        now,
    )

    events = fetch_all(
        args.mode,
        window_start,
        window_end,
    )

    changed = write_feed(
        mode=args.mode,
        now=now,
        events=events,
        output_path=args.output,
        history_path=args.history,
        feed_url=default_feed_url(),
    )

    print(
        f"{len(events)} arrangementer; "
        + (
            "feeden ble oppdatert"
            if changed
            else "ingen ny RSS-oppføring"
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
