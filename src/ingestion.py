# -*- coding: utf-8 -*-
"""
Ingestion Module for Global Macro Time-Series Knowledge Graph
===========================================================
Fetches scripts from target YouTube videos using youtube-transcript-api
with yt-dlp fallback.  No text chunking is applied to maintain the full
1M token context.

👑 [Ver 3.1+4.0 YT-Block Recovery]
- yt-dlp is the PRIMARY fetch path (cookies-free thanks to n-challenge solver).
- youtube-transcript-api is the FAST fallback (skipped entirely when disabled).
- js_runtimes MUST be a dict in yt-dlp ≥2025.05 (was a list in older versions).
- `ejs:github` remote_components is REQUIRED to solve YouTube's n-sig challenge.
- extractor_args youtube:player_client=android,web impersonates a mobile client
  → bypasses web-client transcript endpoint rate-limiting.
- All YouTube HTTP calls are wrapped with a hard timeout to prevent silent hang.
"""

import os
import time
import xml.etree.ElementTree as ET
import http.cookiejar
import requests
from pathlib import Path
from youtube_transcript_api import YouTubeTranscriptApi

from src.config import settings


# Hard timeout for any single YouTube HTTP call (seconds).
# Prevents silent hang on IP block (older youtube-transcript-api ≤0.6 used to
# raise a clean exception; newer versions can deadlock in connection pool).
_YT_HTTP_TIMEOUT = 60


def _build_yt_dlp_opts(cookies_path: Path | None, proxy_url: str | None) -> dict:
    """Build yt-dlp options dict for transcript fetch.

    Ver 3.1+4.0 hardening:
      * js_runtimes is a DICT (yt-dlp ≥2025.05 breaking change)
      * remote_components ejs:github is REQUIRED to load n-challenge solver
      * extractor_args impersonates android+web to bypass web-only rate-limit
      * cookiefile is optional — yt-dlp works without cookies once
        remote_components + js_runtimes are correctly set
    """
    opts: dict = {
        'writeautosub': True,
        'writesubtitles': True,
        'skip_download': True,
        'outtmpl': os.path.join('%(tmpdir)s', 'sub'),
        'subtitleslangs': ['en', 'ko', 'en-US', 'ko-KR'],
        # 👑 [Ver 3.1+] n-challenge solver
        'js_runtimes': {'node': {}},
        'remote_components': ['ejs:github'],
        # 👑 [Ver 3.1+] Impersonate mobile client to bypass web throttling
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web', 'ios'],
                'skip': ['translated_subs', 'dash'],
            }
        },
        'allow_no_formats': True,
        'quiet': True,
        'no_warnings': True,
        # 👑 [Ver 3.0 Backfill Safety] throttled yt-dlp
        'sleep_requests': 1,
        'sleep_interval': 2,
        'max_sleep_interval': 5,
        'retries': 3,
        'fragment_retries': 3,
        # Hard socket-level timeout (defeats silent hang)
        'socket_timeout': _YT_HTTP_TIMEOUT,
    }
    if cookies_path and cookies_path.exists():
        opts['cookiefile'] = str(cookies_path)
    if proxy_url:
        opts['proxy'] = proxy_url
    return opts


def _yt_dlp_fetch(video_id: str, cookies_path: Path | None, proxy_url: str | None) -> str:
    """Fetch transcript via yt-dlp (primary path under Ver 3.1+)."""
    import yt_dlp
    import tempfile
    import glob
    import re

    ydl_opts = _build_yt_dlp_opts(cookies_path, proxy_url)
    with tempfile.TemporaryDirectory() as temp_dir:
        ydl_opts['outtmpl'] = os.path.join(temp_dir, 'sub.%(ext)s')
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            url = f"https://www.youtube.com/watch?v={video_id}"
            ydl.download([url])
        sub_files = glob.glob(os.path.join(temp_dir, 'sub.*'))
        if not sub_files:
            raise RuntimeError("yt-dlp: no subtitle file produced (VTT/SRT absent).")
        # Prefer .vtt, then .srt, then anything
        sub_files.sort(key=lambda p: (0 if p.endswith('.vtt') else 1 if p.endswith('.srt') else 2, p))
        sub_file = sub_files[0]
        with open(sub_file, 'r', encoding='utf-8') as f:
            sub_content = f.read()

    # WebVTT / SRT → text (same parser as before)
    lines = sub_content.split('\n')
    text_lines: list[str] = []
    for line in lines:
        line = line.strip()
        if (not line or
            line.startswith('WEBVTT') or
            line.startswith('Kind:') or
            line.startswith('Language:') or
            '-->' in line or
            line.startswith('Style:') or
            line.isdigit()):
            continue
        clean = re.sub(r'<[^>]+>', '', line).strip()
        if clean:
            text_lines.append(clean)
    # Dedup consecutive identical lines (common in auto-captions)
    deduped: list[str] = []
    for line in text_lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    full_text = " ".join(deduped)
    full_text = " ".join(full_text.split())
    if not full_text:
        raise RuntimeError("yt-dlp: parsed transcript is empty after dedup.")
    return full_text


def get_youtube_transcript(video_id: str) -> str:
    """Retrieve full transcript of a YouTube video.

    Order of attempts (Ver 3.1+ YT-block recovery):
      1. yt-dlp with n-solver + impersonation (cookies-free)
      2. youtube-transcript-api fallback (cookies-aware, may hang on block)

    Retries with exponential backoff (15s → 30s → 60s).
    Hard socket timeout (`_YT_HTTP_TIMEOUT`) prevents silent deadlock.
    """
    if not video_id:
        raise ValueError("Invalid YouTube video ID.")

    project_dir = Path(__file__).resolve().parent.parent
    cookies_file = settings.youtube.cookies_file
    cookies_path = Path(cookies_file)
    if not cookies_path.is_absolute():
        cookies_path = project_dir / cookies_file
    proxy_url = settings.youtube.proxy

    if cookies_path.exists():
        print(f"   [INFO] YouTube cookies found at {cookies_path} (yt-dlp will use them opportunistically)")
    else:
        print("   [INFO] No cookies.txt — relying on yt-dlp n-solver (cookies-free mode)")

    max_retries = 3
    backoff_seconds = 15
    last_error: Exception | None = None

    for attempt in range(max_retries):
        # ── PRIMARY: yt-dlp (n-solver + impersonation) ──────────────────────
        try:
            t0 = time.time()
            text = _yt_dlp_fetch(video_id, cookies_path, proxy_url)
            print(f"   ✓ yt-dlp transcript fetch OK ({len(text):,} chars, {time.time()-t0:.1f}s)")
            return text
        except Exception as e:
            last_error = e
            print(f"   [WARN] yt-dlp attempt {attempt + 1} failed for {video_id}: {e}")

        # ── FALLBACK: youtube-transcript-api ─────────────────────────────────
        if attempt < max_retries - 1:
            try:
                session = requests.Session()
                session.headers.update({
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                    'Accept-Language': 'en-US,en;q=0.9',
                })
                if proxy_url:
                    session.proxies = {"http": proxy_url, "https": proxy_url}
                if cookies_path.exists():
                    try:
                        cj = http.cookiejar.MozillaCookieJar(str(cookies_path))
                        cj.load(ignore_discard=True, ignore_expires=True)
                        session.cookies = cj
                    except Exception as ce:
                        print(f"   [WARN] cookie load failed: {ce}")
                api = YouTubeTranscriptApi(http_client=session)
                try:
                    fetched = api.fetch(video_id, languages=('en', 'ko'))
                except Exception:
                    fetched = api.fetch(video_id)
                data = fetched.to_raw_data()
                full_text = " ".join([item['text'].strip() for item in data if item.get('text')])
                full_text = " ".join(full_text.split())
                if full_text:
                    print(f"   ✓ youtube-transcript-api fallback OK ({len(full_text):,} chars)")
                    return full_text
            except Exception as e2:
                print(f"   [WARN] transcript-api fallback also failed: {e2}")
                last_error = e2

            wait_s = backoff_seconds * (2 ** attempt)
            print(f"   [RETRY] Backing off {wait_s}s before attempt {attempt + 2}...")
            time.sleep(wait_s)
        else:
            break

    raise RuntimeError(
        f"Failed to fetch YouTube transcript for video {video_id} after "
        f"{max_retries} attempts (yt-dlp + transcript-api). Last error: {last_error}"
    )

def fetch_video_ids_from_channel(channel_id: str, max_age_hours: int = 0) -> list[tuple[str, str]]:
    """
    Fetches the latest uploaded video IDs and their published dates from a YouTube channel's RSS feed.
    Filters videos to only those uploaded within the last max_age_hours.
    If max_age_hours is None or 0, retrieves all (up to 15) available videos.
    
    Args:
        channel_id (str): The YouTube channel ID (usually starts with 'UC').
        max_age_hours (int): Limit results to videos published within this many hours.
        
    Returns:
        list[tuple[str, str]]: A list of tuples (video_id, published_date_str_YYYY_MM_DD).
    """
    import requests
    from datetime import datetime, timezone, timedelta
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        resp = requests.get(url, headers=headers, timeout=15)
        
        if resp.status_code != 200:
            print(f"[WARN] Failed to fetch RSS feed for channel {channel_id}: Status {resp.status_code}")
            print(f"[DEBUG] Response body (first 300 chars): {resp.text[:300]}")
            return []
            
        root = ET.fromstring(resp.content)
        
        # Atom and YouTube XML Namespaces
        ns = {
            'atom': 'http://www.w3.org/2005/Atom',
            'yt': 'http://www.youtube.com/xml/schemas/2015'
        }
        
        now_utc = datetime.now(timezone.utc)
        video_info_list = []
        
        for entry in root.findall('atom:entry', ns):
            video_id_el = entry.find('yt:videoId', ns)
            published_el = entry.find('atom:published', ns)
            
            if video_id_el is not None and video_id_el.text:
                video_id = video_id_el.text.strip()
                
                # Default date format YYYY-MM-DD
                pub_date = now_utc.strftime("%Y-%m-%d")
                is_recent = True
                
                if published_el is not None and published_el.text:
                    try:
                        pub_str = published_el.text.strip()
                        # Capture only YYYY-MM-DD part for backlink mapping
                        pub_date = pub_str.split("T")[0]
                        
                        if pub_str.endswith('Z'):
                            pub_str = pub_str[:-1] + '+00:00'
                        pub_time = datetime.fromisoformat(pub_str)
                        
                        if max_age_hours and now_utc - pub_time > timedelta(hours=max_age_hours):
                            is_recent = False
                    except Exception as te:
                        print(f"[WARN] Failed to parse published time '{published_el.text}' for video {video_id}: {te}")
                
                if is_recent:
                    video_info_list.append((video_id, pub_date))
                    
        return video_info_list
    except Exception as e:
        print(f"[WARN] Failed to parse RSS feed for channel {channel_id}: {e}")
        return []

if __name__ == "__main__":
    # Quick standalone unit test
    import sys
    test_id = "uMMwAbYSmr4" if len(sys.argv) < 2 else sys.argv[1]
    print(f"Testing Ingestion Module with video ID: {test_id}")
    try:
        script = get_youtube_transcript(test_id)
        print(f"Success! Retrieved {len(script)} characters.")
        print(f"Sample: {script[:300]}...")
    except Exception as err:
        print(f"Error: {err}")
        
    print("\nTesting Channel RSS Ingestion...")
    # Test with CNBC channel ID
    cnbc_channel = "UCvJJ_dzjViJCoLf5uKUTwoA"
    vids = fetch_video_ids_from_channel(cnbc_channel, max_age_hours=24)
    print(f"CNBC latest videos (past 24h): {vids}")
