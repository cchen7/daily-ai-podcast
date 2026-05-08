from __future__ import annotations

import argparse
import email.utils
import html
import os
import re
import sys
import textwrap
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
        if line.startswith("## "):
            heading = strip_markdown(line[3:]).strip()
            if heading.lower() not in {"top stories", "signals to watch", "bottom line"}:
                lines.append(f"{heading}.")
            continue
        if line.startswith("### "):
            title = strip_markdown(line[4:]).strip()
            lines.append(f"Next: {title}.")
            continue

        line = strip_markdown(line)
        line = line.replace("Why it matters:", "Why it matters.")
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)

    date_obj = datetime.strptime(episode_date, "%Y-%m-%d").date()
    spoken_date = date_obj.strftime("%A, %B %-d, %Y") if sys.platform != "win32" else date_obj.strftime("%A, %B %#d, %Y")
    body = "\n\n".join(lines)
    return textwrap.dedent(
        f"""
        Welcome to {podcast_title}, your concise briefing on the AI news that matters. Today is {spoken_date}.

        {body}

        That's it for today's Daily AI Brief. The main signal: AI is moving beyond model launches into governance, infrastructure, workforce design, and legal accountability. Thanks for listening.
        """
    ).strip()


def script_to_ssml(script: str, config: PodcastConfig) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", script) if p.strip()]
    body = "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)
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


def channel_text(channel: ET.Element, tag: str, value: str) -> None:
    node = channel.find(tag)
    if node is None:
        node = ET.SubElement(channel, tag)
    node.text = value


def build_or_update_feed(config: PodcastConfig, episode: Episode, feed_url: str) -> ET.ElementTree:
    root = fetch_existing_feed(feed_url)
    if root is None or root.tag != "rss":
        root = ET.Element("rss", {"version": "2.0"})
        channel = ET.SubElement(root, "channel")
    else:
        channel = root.find("channel")
        if channel is None:
            channel = ET.SubElement(root, "channel")

    channel_text(channel, "title", config.title)
    channel_text(channel, "link", config.public_base_url)
    channel_text(channel, "description", config.description)
    channel_text(channel, "language", config.language)
    channel_text(channel, "copyright", f"Copyright {datetime.now().year} {config.author}")
    channel_text(channel, f"{{{ITUNES_NS}}}author", config.author)
    channel_text(channel, f"{{{ITUNES_NS}}}summary", config.description)
    channel_text(channel, f"{{{ITUNES_NS}}}explicit", config.explicit)

    owner = channel.find(f"{{{ITUNES_NS}}}owner")
    if owner is None:
        owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    channel_text(owner, f"{{{ITUNES_NS}}}name", config.owner_name)
    channel_text(owner, f"{{{ITUNES_NS}}}email", config.owner_email)

    category = channel.find(f"{{{ITUNES_NS}}}category")
    if category is None:
        category = ET.SubElement(channel, f"{{{ITUNES_NS}}}category")
    category.set("text", config.category)

    if config.image_url:
        image = channel.find("image")
        if image is None:
            image = ET.SubElement(channel, "image")
        channel_text(image, "url", config.image_url)
        channel_text(image, "title", config.title)
        channel_text(image, "link", config.public_base_url)

        itunes_image = channel.find(f"{{{ITUNES_NS}}}image")
        if itunes_image is None:
            itunes_image = ET.SubElement(channel, f"{{{ITUNES_NS}}}image")
        itunes_image.set("href", config.image_url)

    atom_link = channel.find(f"{{{ATOM_NS}}}link")
    if atom_link is None:
        atom_link = ET.SubElement(channel, f"{{{ATOM_NS}}}link")
    atom_link.set("href", feed_url)
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    guid = episode.mp3_url
    for item in list(channel.findall("item")):
        guid_node = item.find("guid")
        if guid_node is not None and guid_node.text == guid:
            channel.remove(item)

    item = ET.Element("item")
    channel_text(item, "title", episode.title)
    channel_text(item, "description", episode.description)
    channel_text(item, "pubDate", email.utils.format_datetime(episode.pub_date))
    channel_text(item, "guid", guid)
    item.find("guid").set("isPermaLink", "false")
    channel_text(item, f"{{{ITUNES_NS}}}author", config.author)
    channel_text(item, f"{{{ITUNES_NS}}}summary", episode.description)
    channel_text(item, f"{{{ITUNES_NS}}}duration", episode.duration)
    channel_text(item, f"{{{ITUNES_NS}}}explicit", config.explicit)
    enclosure = ET.SubElement(item, "enclosure")
    enclosure.set("url", episode.mp3_url)
    enclosure.set("length", str(episode.file_size))
    enclosure.set("type", "audio/mpeg")
    channel.insert(0, item)

    channel_text(channel, "lastBuildDate", email.utils.format_datetime(datetime.now(timezone.utc)))
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
