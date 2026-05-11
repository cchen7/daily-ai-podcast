from __future__ import annotations

import argparse
import email.utils
import html
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, time, timezone, timedelta
from pathlib import Path

import azure.cognitiveservices.speech as speechsdk
from azure.storage.blob import BlobServiceClient, ContentSettings
from dotenv import load_dotenv
from mutagen.mp3 import MP3


ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)


@dataclass(frozen=True)
class PodcastConfig:
    title: str
    description: str
    author: str
    owner_name: str
    owner_email: str
    language: str
    category: str
    explicit: str
    image_url: str
    public_base_url: str
    storage_container: str
    speech_key: str
    speech_region: str
    speech_voice: str
    speech_rate: str
    speech_pitch: str
    storage_connection_string: str


@dataclass(frozen=True)
class Episode:
    date: str
    title: str
    description: str
    slug: str
    pub_date: datetime
    mp3_path: Path
    mp3_url: str
    duration: str
    file_size: int


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def require_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> PodcastConfig:
    load_dotenv()
    return PodcastConfig(
        title=env("PODCAST_TITLE", "The Daily AI Brief"),
        description=env("PODCAST_DESCRIPTION", "A concise daily briefing on the AI news that matters."),
        author=env("PODCAST_AUTHOR", "Clawpilot"),
        owner_name=env("PODCAST_OWNER_NAME", "Clawpilot"),
        owner_email=env("PODCAST_OWNER_EMAIL", "you@example.com"),
        language=env("PODCAST_LANGUAGE", "en-us"),
        category=env("PODCAST_CATEGORY", "Technology"),
        explicit=env("PODCAST_EXPLICIT", "false").lower(),
        image_url=env("PODCAST_IMAGE_URL"),
        public_base_url=require_env("PODCAST_PUBLIC_BASE_URL").rstrip("/"),
        storage_container=env("AZURE_STORAGE_CONTAINER", "podcast"),
        speech_key=require_env("AZURE_SPEECH_KEY"),
        speech_region=require_env("AZURE_SPEECH_REGION"),
        speech_voice=env("AZURE_SPEECH_VOICE", "en-US-AvaMultilingualNeural"),
        speech_rate=env("AZURE_SPEECH_RATE", "0%"),
        speech_pitch=env("AZURE_SPEECH_PITCH", "0%"),
        storage_connection_string=require_env("AZURE_STORAGE_CONNECTION_STRING"),
    )


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "episode"


def strip_markdown(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"[*_~>#]", "", text)
    return text


_NUM_TO_WORD = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    11: "eleven", 12: "twelve",
}
_STORY_NUMBER = re.compile(r"^(\d+)\.\s+(.*)$")


def _conversational_h3(title: str) -> str:
    """Turn '1. OpenAI's governance drama...' into 'Story one. OpenAI's governance drama...'"""
    match = _STORY_NUMBER.match(title)
    if match:
        n = int(match.group(1))
        rest = match.group(2)
        ordinal = _NUM_TO_WORD.get(n, str(n))
        return f"Story {ordinal}. {rest}."
    return f"Next up. {title}."


_H2_LEAD_INS = {
    "top stories": None,  # implicit; H3s carry the structure
    "signals to watch": "Now, a few signals to keep an eye on.",
    "bottom line": "And the bottom line.",
}


def _why_matters_replacement(match: re.Match[str]) -> str:
    first, second = match.group(1), match.group(2)
    # If next char is also uppercase (acronym like "AI"), preserve case;
    # otherwise lowercase the leading letter to read as a continuation.
    if second.islower():
        first = first.lower()
    return f"So why does this matter? Well, {first}{second}"


def newsletter_to_script(newsletter: str, episode_date: str, podcast_title: str) -> str:
    lines: list[str] = []
    skip_sources = False

    for raw_line in newsletter.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("source:") or lower.startswith("sources:") or line.startswith("http"):
            continue
        if lower in {"## source links", "## sources", "source links"}:
            skip_sources = True
            continue
        if skip_sources and (line.startswith("|") or line.startswith("---")):
            continue
        if line.startswith("# "):
            continue
        # Skip date/locale subtitle rows (e.g. "Friday, May 8, 2026 · Asia/Shanghai").
        if " · " in line and not line.startswith(("##", "-", "*")):
            continue
        if line.startswith("## "):
            heading = strip_markdown(line[3:]).strip()
            lead_in = _H2_LEAD_INS.get(heading.lower(), f"{heading}.")
            if lead_in:
                lines.append(lead_in)
            continue
        if line.startswith("### "):
            title = strip_markdown(line[4:]).strip()
            lines.append(_conversational_h3(title))
            continue

        line = strip_markdown(line)
        line = re.sub(r"Why it matters:\s*([A-Za-z])([A-Za-z])", _why_matters_replacement, line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)

    date_obj = datetime.strptime(episode_date, "%Y-%m-%d").date()
    spoken_date = date_obj.strftime("%A, %B %-d, %Y") if sys.platform != "win32" else date_obj.strftime("%A, %B %#d, %Y")
    intro = (
        f"Hey — welcome to {podcast_title}. "
        f"I'm catching you up on the AI news that actually matters today. "
        f"It's {spoken_date}."
    )
    outro = "That's it for today. Thanks for listening — see you tomorrow."
    return "\n\n".join([intro, "\n\n".join(lines), outro])


def script_to_ssml(script: str, config: PodcastConfig) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", script) if p.strip()]
    body = '<break time="450ms"/>'.join(
        f"<p>{html.escape(p)}</p>" for p in paragraphs
    )
    return (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="en-US">'
        f'<voice name="{html.escape(config.speech_voice)}">'
        f'<prosody rate="{html.escape(config.speech_rate)}" pitch="{html.escape(config.speech_pitch)}">'
        f"{body}"
        f"</prosody></voice></speak>"
    )


def synthesize_mp3(script: str, output_path: Path, config: PodcastConfig) -> None:
    speech_config = speechsdk.SpeechConfig(subscription=config.speech_key, region=config.speech_region)
    speech_config.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Audio24Khz160KBitRateMonoMp3
    )
    audio_config = speechsdk.audio.AudioOutputConfig(filename=str(output_path))
    synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
    result = synthesizer.speak_ssml_async(script_to_ssml(script, config)).get()

    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        return
    if result.reason == speechsdk.ResultReason.Canceled:
        details = speechsdk.SpeechSynthesisCancellationDetails(result)
        raise RuntimeError(f"Speech synthesis canceled: {details.reason}; {details.error_details}")
    raise RuntimeError(f"Speech synthesis failed: {result.reason}")


def mp3_duration(path: Path) -> str:
    audio = MP3(path)
    seconds = int(round(audio.info.length))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def upload_blob(local_path: Path, blob_name: str, content_type: str, config: PodcastConfig) -> str:
    service = BlobServiceClient.from_connection_string(config.storage_connection_string)
    blob = service.get_blob_client(container=config.storage_container, blob=blob_name)
    with local_path.open("rb") as handle:
        blob.upload_blob(
            handle,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )
    return f"{config.public_base_url}/{blob_name}"


def fetch_existing_feed(feed_url: str) -> ET.Element | None:
    try:
        with urllib.request.urlopen(feed_url, timeout=20) as response:
            if response.status == 200:
                return ET.fromstring(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, ET.ParseError):
        return None
    return None


_DATE_IN_URL = re.compile(r"/episodes/(\d{4}-\d{2}-\d{2})-")


def build_or_update_feed(config: PodcastConfig, episode: Episode, feed_url: str) -> ET.ElementTree:
    existing_root = fetch_existing_feed(feed_url)
    preserved_items: list[ET.Element] = []
    if existing_root is not None and existing_root.tag == "rss":
        existing_channel = existing_root.find("channel")
        if existing_channel is not None:
            for old in existing_channel.findall("item"):
                guid_node = old.find("guid")
                guid_text = guid_node.text if guid_node is not None else None
                if guid_text == episode.mp3_url:
                    continue
                match = _DATE_IN_URL.search(guid_text or "")
                if match and match.group(1) == episode.date:
                    continue
                preserved_items.append(old)

    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")

    ET.SubElement(channel, "title").text = config.title
    ET.SubElement(channel, "link").text = config.public_base_url
    ET.SubElement(channel, "description").text = config.description
    ET.SubElement(channel, "language").text = config.language
    ET.SubElement(channel, "copyright").text = f"Copyright {datetime.now().year} {config.author}"
    ET.SubElement(channel, f"{{{ITUNES_NS}}}author").text = config.author
    ET.SubElement(channel, f"{{{ITUNES_NS}}}summary").text = config.description
    ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = config.explicit
    ET.SubElement(channel, f"{{{ITUNES_NS}}}type").text = "episodic"

    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    ET.SubElement(owner, f"{{{ITUNES_NS}}}name").text = config.owner_name
    ET.SubElement(owner, f"{{{ITUNES_NS}}}email").text = config.owner_email

    ET.SubElement(channel, f"{{{ITUNES_NS}}}category", {"text": config.category})

    if config.image_url:
        image = ET.SubElement(channel, "image")
        ET.SubElement(image, "url").text = config.image_url
        ET.SubElement(image, "title").text = config.title
        ET.SubElement(image, "link").text = config.public_base_url
        ET.SubElement(channel, f"{{{ITUNES_NS}}}image", {"href": config.image_url})

    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {"href": feed_url, "rel": "self", "type": "application/rss+xml"},
    )

    ET.SubElement(channel, "lastBuildDate").text = email.utils.format_datetime(
        datetime.now(timezone.utc)
    )

    item = ET.Element("item")
    ET.SubElement(item, "title").text = episode.title
    ET.SubElement(item, "description").text = episode.description
    ET.SubElement(item, "pubDate").text = email.utils.format_datetime(episode.pub_date)
    guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
    guid.text = episode.mp3_url
    ET.SubElement(item, f"{{{ITUNES_NS}}}author").text = config.author
    ET.SubElement(item, f"{{{ITUNES_NS}}}summary").text = episode.description
    ET.SubElement(item, f"{{{ITUNES_NS}}}duration").text = episode.duration
    ET.SubElement(item, f"{{{ITUNES_NS}}}explicit").text = config.explicit
    ET.SubElement(
        item,
        "enclosure",
        {"url": episode.mp3_url, "length": str(episode.file_size), "type": "audio/mpeg"},
    )

    channel.append(item)
    for old in preserved_items:
        channel.append(old)

    return ET.ElementTree(root)


def validate_url(url: str, expected_content_type: str) -> None:
    request = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        content_type = response.headers.get("Content-Type", "")
        if expected_content_type not in content_type:
            raise RuntimeError(f"{url} returned Content-Type {content_type}, expected {expected_content_type}")


def publish(args: argparse.Namespace) -> None:
    config = load_config()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    newsletter = Path(args.newsletter).read_text(encoding="utf-8")
    script = newsletter_to_script(newsletter, args.date, config.title)
    script_path = output_dir / f"{args.date}-script.txt"
    script_path.write_text(script, encoding="utf-8")

    slug = slugify(args.title)
    mp3_path = output_dir / f"{args.date}-{slug}.mp3"
    synthesize_mp3(script, mp3_path, config)

    mp3_blob = f"episodes/{mp3_path.name}"
    mp3_url = upload_blob(mp3_path, mp3_blob, "audio/mpeg", config)

    local_tz = timezone(timedelta(hours=8))
    pub_date = datetime.combine(datetime.strptime(args.date, "%Y-%m-%d").date(), time(8, 0), tzinfo=local_tz)
    episode = Episode(
        date=args.date,
        title=args.title,
        description=args.description or config.description,
        slug=slug,
        pub_date=pub_date,
        mp3_path=mp3_path,
        mp3_url=mp3_url,
        duration=mp3_duration(mp3_path),
        file_size=mp3_path.stat().st_size,
    )

    feed_url = f"{config.public_base_url}/podcast.xml"
    feed_tree = build_or_update_feed(config, episode, feed_url)
    rss_path = output_dir / "podcast.xml"
    feed_tree.write(rss_path, encoding="utf-8", xml_declaration=True)
    upload_blob(rss_path, "podcast.xml", "application/rss+xml", config)

    validate_url(mp3_url, "audio/mpeg")
    validate_url(feed_url, "application/rss+xml")

    print(f"script={script_path}")
    print(f"mp3={mp3_path}")
    print(f"mp3_url={mp3_url}")
    print(f"rss={rss_path}")
    print(f"rss_url={feed_url}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish the Daily AI Brief as a podcast episode.")
    parser.add_argument("--date", required=True, help="Episode date in YYYY-MM-DD format.")
    parser.add_argument("--newsletter", required=True, help="Path to newsletter markdown/text.")
    parser.add_argument("--title", default="Daily AI Brief", help="Episode title.")
    parser.add_argument("--description", default="", help="Episode description.")
    parser.add_argument("--output-dir", default="output", help="Local output directory.")
    args = parser.parse_args()
    publish(args)


if __name__ == "__main__":
    main()
