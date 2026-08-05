#!/usr/bin/env python3
"""
Ver 3.1 — Channel ID Validator
================================
Probes every channel_id in configs/channels.json against YouTube's RSS feed
endpoint (`feeds/videos.xml?channel_id=...`) to detect:

  - VALID:    200 + at least one <entry> in the response body
  - EMPTY:    200 but no <entry> (channel exists but no recent videos)
  - INVALID:  non-200 (404 / redirect / 403)
  - TIMEOUT:  request exceeded --timeout seconds

Results are written to `data/channel_validation.csv` and printed as a
summary table.  Exit code:
  0  → all channels VALID or EMPTY
  1  → at least one INVALID or TIMEOUT

Usage:
  python scripts/validate_channels.py
  python scripts/validate_channels.py --tiers tier_1_highest_density
  python scripts/validate_channels.py --timeout 15
  python scripts/validate_channels.py --csv /tmp/channels_check.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANNELS_JSON = PROJECT_ROOT / "configs" / "channels.json"
DEFAULT_CSV = PROJECT_ROOT / "data" / "channel_validation.csv"

# YouTube RSS endpoint
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
ATOM_NS = "{http://www.w3.org/2005/Atom}"


def load_channels(channels_json: Path, tier_filter: list[str] | None, include_disabled: bool) -> list[tuple[str, str, str, str, bool]]:
    """Return [(tier, name, channel_id, focus, enabled), ...] from channels.json."""
    with open(channels_json, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    out: list[tuple[str, str, str, str, bool]] = []
    for tier_key, tier_data in cfg.get("tiers", {}).items():
        if tier_filter and tier_key not in tier_filter:
            continue
        enabled = tier_data.get("_enabled", True) is not False
        if not include_disabled and not enabled:
            continue
        for ch in tier_data.get("channels", []):
            out.append((tier_key, ch["name"], ch["channel_id"], ch.get("focus", ""), enabled))
    return out


def validate_channel(channel_id: str, timeout: int, max_retries: int = 2) -> tuple[str, int | None, str]:
    """Probe a single channel_id.  Returns (status, http_code, detail).

    YouTube RSS rate-limits aggressively when many requests come from the
    same IP in a short window, returning 404 for valid IDs.  We retry
    with exponential backoff and rotate the User-Agent string to look
    less like a single client.
    """
    url = RSS_URL.format(cid=channel_id)
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    ]
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        ua = user_agents[attempt % len(user_agents)]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "en-US"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.getcode()
                body = resp.read(64 * 1024)  # Cap at 64 KB; we only need <entry> count
            # 👑 [A26] urlopen 은 non-2xx 시 HTTPError raise → 이전 `if code != 200`
            # 분기는 도달 불가(dead code)라 제거. 200 경로만 Atom 파싱.
            # Atom feed: count <entry> elements
            try:
                root = ET.fromstring(body)
            except ET.ParseError as e:
                return ("INVALID", code, f"parse_error: {e}")
            entry_count = len(root.findall(f"{ATOM_NS}entry"))
            if entry_count == 0:
                return ("EMPTY", code, "0_entries")
            return ("VALID", code, f"{entry_count}_entries")
        except urllib.error.HTTPError as e:
            last_exc = e
        except urllib.error.URLError as e:
            last_exc = e
        except TimeoutError:
            last_exc = TimeoutError(f"timeout_{timeout}s")
        except Exception as e:  # noqa: BLE001
            last_exc = e
        # Backoff before retry
        if attempt < max_retries:
            time.sleep(2 ** attempt)

    # All retries exhausted
    if isinstance(last_exc, urllib.error.HTTPError):
        return ("INVALID", last_exc.code, f"http_{last_exc.code}_after_{max_retries + 1}_tries")
    if isinstance(last_exc, TimeoutError):
        return ("TIMEOUT", None, f"timeout_{timeout}s_after_{max_retries + 1}_tries")
    if isinstance(last_exc, urllib.error.URLError):
        return ("INVALID", None, f"url_error: {last_exc.reason}_after_{max_retries + 1}_tries")
    return ("INVALID", None, f"exception: {type(last_exc).__name__ if last_exc else 'Unknown'}: {last_exc}_after_{max_retries + 1}_tries")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate YouTube channel IDs in configs/channels.json")
    parser.add_argument("--channels-json", default=str(DEFAULT_CHANNELS_JSON), help="Path to channels.json")
    parser.add_argument("--tiers", default="all", help="Comma-separated tier names, or 'all'")
    parser.add_argument("--include-disabled", action="store_true",
                        help="Also probe channels in tiers marked _enabled=false (for ID discovery)")
    parser.add_argument("--timeout", type=int, default=10, help="HTTP timeout in seconds")
    parser.add_argument("--delay", type=float, default=2.0, help="Inter-request delay (be polite; YouTube RSS rate-limits hard)")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Output CSV path")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-channel print; only summary")
    args = parser.parse_args()

    if args.tiers == "all":
        tier_filter = None
    else:
        tier_filter = [t.strip() for t in args.tiers.split(",") if t.strip()]

    channels = load_channels(Path(args.channels_json), tier_filter, args.include_disabled)
    if not channels:
        print(f"[ERROR] No channels found for tiers={args.tiers} in {args.channels_json}")
        return 1

    print(f"Validating {len(channels)} channel(s) from {Path(args.channels_json).name}...")
    print(f"  tiers             = {args.tiers}")
    print(f"  include_disabled  = {args.include_disabled}")
    print(f"  timeout           = {args.timeout}s")
    print(f"  delay             = {args.delay}s")
    print(f"  csv output        = {args.csv}")
    print("-" * 80)

    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {"VALID": 0, "EMPTY": 0, "INVALID": 0, "TIMEOUT": 0}
    bad_rows: list[tuple[str, str, str, str, str, int | None, str]] = []

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["tier", "name", "channel_id", "focus", "tier_enabled", "status", "http_code", "detail"])
        for tier, name, cid, focus, tier_enabled in channels:
            status, code, detail = validate_channel(cid, args.timeout)
            counts[status] = counts.get(status, 0) + 1
            writer.writerow([tier, name, cid, focus, "yes" if tier_enabled else "no", status, code or "", detail])
            if not args.quiet:
                enab_marker = "E" if tier_enabled else "d"  # E=Enabled, d=disabled
                marker = {"VALID": "✓", "EMPTY": "·", "INVALID": "✗", "TIMEOUT": "⏱"}[status]
                print(f"  {enab_marker}{marker} [{tier:32s}] {name:32s} {cid}  → {status} ({detail})")
            if status in ("INVALID", "TIMEOUT"):
                bad_rows.append((tier, name, cid, focus, status, code, detail))
            time.sleep(args.delay)

    print("-" * 80)
    print("Summary:")
    for k in ("VALID", "EMPTY", "INVALID", "TIMEOUT"):
        print(f"  {k:8s}: {counts.get(k, 0)}")
    print(f"\nCSV written to: {csv_path}")

    if bad_rows:
        print("\n[!] Bad channels (need fixing in configs/channels.json):")
        for tier, name, cid, focus, status, code, detail in bad_rows:
            print(f"  - {tier} / {name} ({cid}) → {status} {code} {detail}")
        return 1
    print("\n✓ All channels are reachable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
