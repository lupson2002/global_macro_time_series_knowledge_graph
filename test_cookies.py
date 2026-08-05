import os
import http.cookiejar
import requests
from youtube_transcript_api import YouTubeTranscriptApi

def test():
    video_id = "n6jpbsXCGyg"
    session = requests.Session()
    
    # 1. User-Agent 헤더 추가
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    })
    
    cookies_path = "/home/mikey/global_macro_time_series_knowledge_graph/cookies.txt"
    if os.path.exists(cookies_path):
        cookie_jar = http.cookiejar.MozillaCookieJar(cookies_path)
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies = cookie_jar
        print("✓ Loaded cookies")
    else:
        print("✗ No cookies found")

    try:
        api = YouTubeTranscriptApi(http_client=session)
        print("Attempting to fetch transcript with custom headers...")
        fetched = api.fetch(video_id, languages=('en', 'ko'))
        data = fetched.to_raw_data()
        full_text = " ".join([item['text'].strip() for item in data if item.get('text')])
        print(f"✓ Success! Characters fetched: {len(full_text)}")
        print(f"Sample: {full_text[:200]}...")
    except Exception as e:
        print(f"✗ Failed: {e}")

if __name__ == "__main__":
    test()
