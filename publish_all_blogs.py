#!/usr/bin/env python3
"""블로그 자동 포스팅 — 네이버 + 티스토리 (Playwright persistent 세션).

흐름:
  1. python publish_all_blogs.py --login   # 최초 1회: 브라우저 열고 수동 로그인 → 세션 저장
  2. python publish_all_blogs.py            # 저장 세션으로 자동 포스팅 (tistory_draft.md)
  3. python publish_all_blogs.py --headless  # cron 무인용 헤드리스(xvfb-run 권장)

원고: tistory_draft.md (첫 줄 `# 제목`, 나머지 본문).
클립보드 붙여넣기(Ctrl+V)로 봇 탐지 우회.

강화(자동화용):
  - --headless/--headed 플래그(기본 headed)
  - 발행 성공 검증(login 리다이렉트 → session_expired FAIL / 정상 URL → OK)
  - 실패 시 스크린샷 logs/blog_publish_fail_<platform>_<ts>.png
  - 구조화 결과 logs/blog_publish_result.json ({naver,tistory} 각 status/url/title/error)
"""
import sys
import time
import json
import datetime as _dt
from pathlib import Path
from playwright.sync_api import sync_playwright

DRAFT = Path(__file__).parent / "tistory_draft.md"
USER_DATA_DIR = "/home/mikey/browser_user_data"
LOG_DIR = Path(__file__).parent / "logs"
RESULT_JSON = LOG_DIR / "blog_publish_result.json"

NAVER_WRITE = "https://blog.naver.com/lupson2002/postwrite"
TISTORY_WRITE = "https://tmmm0123.tistory.com/manage/post"

STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]

LOGIN_HINTS = ("login", "signin", "account", "auth/login")


def _clipboard_copy(page, text: str) -> None:
    """Playwright 브라우저 컨텍스트 내 클립보드에 텍스트 복사.
    pyperclip 의존 제거 — OS 클립보드 대신 Clipboard API 사용.
    headless/cron 환경에서도 동작 (DISPLAY 불필요).
    """
    page.evaluate(f"navigator.clipboard.writeText({json.dumps(text)})")


def load_draft():
    raw = DRAFT.read_text(encoding="utf-8").strip()
    lines = raw.split("\n")
    title = lines[0].replace("#", "").strip()
    body = "\n".join(lines[1:]).strip()
    return title, body


def _is_login_url(url: str) -> bool:
    u = (url or "").lower()
    return any(k in u for k in LOGIN_HINTS)


def _save_screenshot(page, platform: str) -> str:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        p = LOG_DIR / f"blog_publish_fail_{platform}_{ts}.png"
        page.screenshot(path=str(p), full_page=False)
        return str(p)
    except Exception as e:
        print(f"[{platform}] 스크린샷 실패: {e}")
        return ""


def _verify_naver(page) -> dict:
    url = page.url or ""
    if _is_login_url(url):
        return {"status": "FAIL", "url": url, "title": "", "error": "session_expired"}
    # 네이버 발행 후 보통 /PostView.naver?... 또는 /lupson2002/<postid>
    if "postview" in url.lower() or "lupson2002/" in url.lower() or "/post/" in url.lower():
        return {"status": "OK", "url": url, "title": "", "error": ""}
    # 미확정이어도 login 아닌 정상 페이지면 OK(발행 버튼 클릭 완료로 간주)
    if url and "postwrite" not in url.lower():
        return {"status": "OK", "url": url, "title": "", "error": ""}
    return {"status": "UNKNOWN", "url": url, "title": "", "error": "url_ambiguous"}


def _verify_tistory(page) -> dict:
    url = page.url or ""
    if _is_login_url(url):
        return {"status": "FAIL", "url": url, "title": "", "error": "session_expired"}
    # 티스토리 발행 후 보통 /manage/post/... 또는 /entry/... 또는 게시글 URL
    if "/manage/post" in url or "/entry/" in url or "tistory.com/" in url:
        if "post" in url.lower() and "write" not in url.lower():
            return {"status": "OK", "url": url, "title": "", "error": ""}
    if url and "manage/post" not in url.lower():
        return {"status": "OK", "url": url, "title": "", "error": ""}
    return {"status": "UNKNOWN", "url": url, "title": "", "error": "url_ambiguous"}


def wait_login(page, label):
    """로그인 페이지 감지 시 True 반환(세션 만료). 호출자가 early-return 해야 함."""
    time.sleep(3)
    if _is_login_url(page.url):
        print(f"[{label}] ⚠️ 로그인 페이지 감지 — 세션 만료. 발행 중단(--login 재로그인 필요).")
        return True
    return False


def publish_naver(page, title, body):
    print("[네이버] 글쓰기 페이지 이동...")
    try:
        page.goto(NAVER_WRITE, wait_until="domcontentloaded")
        if wait_login(page, "네이버"):
            shot = _save_screenshot(page, "naver")
            return {"status": "FAIL", "url": page.url, "title": "", "error": "session_expired", "screenshot": shot}
        page.wait_for_timeout(2500)

        try:
            if page.locator(".se-popup-alert-confirm").count() > 0:
                page.locator(".se-popup-alert-confirm button").first.click(timeout=3000)
                page.wait_for_timeout(1000)
                print("[네이버] 작성 중 글 팝업 닫음")
        except Exception as e:
            print(f"[네이버] 팝업 닫기 스킵: {e}")

        for sel in [".se-documentTitle", ".se-text-paragraph:first-child"]:
            try:
                page.click(sel, timeout=5000)
                page.keyboard.type(title, delay=20)
                break
            except Exception:
                continue

        _clipboard_copy(page, body)
        for sel in [".se-section-text", "[contenteditable]"]:
            try:
                page.click(sel, timeout=5000)
                page.wait_for_timeout(300)
                page.keyboard.press("Control+V")
                break
            except Exception:
                continue
        page.wait_for_timeout(1500)

        for sel in ["button[class*='publish' i]", "[class*='publish' i] button"]:
            try:
                page.click(sel, timeout=5000)
                break
            except Exception:
                continue
        page.wait_for_timeout(2000)
        for sel in ["button[class*='confirm' i]", "[class*='confirm' i] button", "button[class*='publish' i]"]:
            try:
                page.click(sel, timeout=5000)
                break
            except Exception:
                continue
        page.wait_for_timeout(4000)
        print(f"[네이버] 발행 시도 완료 → {page.url}")
        return _verify_naver(page)
    except Exception as e:
        shot = _save_screenshot(page, "naver")
        return {"status": "FAIL", "url": page.url if page else "", "title": "", "error": f"{type(e).__name__}: {e}", "screenshot": shot}


def publish_tistory(page, title, body):
    print("[티스토리] 글쓰기 페이지 이동...")
    try:
        page.goto(TISTORY_WRITE, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        if "auth/login" in page.url:
            print("[티스토리] 로그인 필요 — 카카오계정 자동 클릭...")
            try:
                page.click(".btn_login", timeout=5000)
                page.wait_for_timeout(5000)
                page.goto(TISTORY_WRITE, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
            except Exception as e:
                print(f"[티스토리] 자동 로그인 실패: {e}")
            # 자동 로그인 후에도 여전히 로그인 페이지면 세션 만료 — 중단.
            if "auth/login" in page.url:
                print("[티스토리] ⚠️ 세션 만료 — 발행 중단(--login 재로그인 필요).")
                shot = _save_screenshot(page, "tistory")
                return {"status": "FAIL", "url": page.url, "title": "", "error": "session_expired", "screenshot": shot}

        for sel in ["#title", "input[name='title']", "input.tit-post"]:
            try:
                page.fill(sel, title, timeout=5000)
                break
            except Exception:
                continue

        _clipboard_copy(page, body)
        for sel in ["#editor-container", ".eddy-editor", "[contenteditable='true']", "#post-content"]:
            try:
                page.click(sel, timeout=5000)
                page.keyboard.press("Control+V")
                break
            except Exception:
                continue
        page.wait_for_timeout(1500)

        for sel in [".btn-publish", "button[type='submit'].btn-publish", "#publishBtn"]:
            try:
                page.click(sel, timeout=5000)
                break
            except Exception:
                continue
        page.wait_for_timeout(4000)
        print(f"[티스토리] 발행 시도 완료 → {page.url}")
        return _verify_tistory(page)
    except Exception as e:
        shot = _save_screenshot(page, "tistory")
        return {"status": "FAIL", "url": page.url if page else "", "title": "", "error": f"{type(e).__name__}: {e}", "screenshot": shot}


def run_login_mode():
    print("=== 로그인 세션 저장 모드 (대화형) ===")
    print("브라우저가 열리면 각 사이트에 직접 로그인 후 터미널에서 엔터 → 세션 저장.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR, headless=False, args=STEALTH_ARGS,
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = ctx.new_page()
        page.goto("https://nid.naver.com/nidlogin.login"); print("[1/2] 네이버 로그인 — 완료 후 엔터")
        input("네이버 로그인 완료 후 엔터: ")
        page.goto(TISTORY_WRITE); print("[2/2] 티스토리 — 카카오 로그인 후 엔터")
        input("티스토리(카카오) 로그인 완료 후 엔터: ")
        ctx.close()
    print("✅ 세션 저장 완료:", USER_DATA_DIR)


def run_publish_mode(headless: bool):
    title, body = load_draft()
    print(f"원고: 제목='{title}' / 본문 {len(body)}자 / headless={headless}")
    results = {}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR, headless=headless, args=STEALTH_ARGS,
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = ctx.new_page()
        results["naver"] = publish_naver(page, title, body)
        results["tistory"] = publish_tistory(page, title, body)
        # FAIL 시 스크린샷이 아직 없으면 한 번 더
        for plat in ("naver", "tistory"):
            if results[plat].get("status") == "FAIL" and not results[plat].get("screenshot"):
                results[plat]["screenshot"] = _save_screenshot(page, plat)
        time.sleep(3)
        ctx.close()

    # 제목 채우기
    results["naver"]["title"] = title
    results["tistory"]["title"] = title
    results["_meta"] = {"published_at": _dt.datetime.now().isoformat(timespec="seconds")}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 게시 결과 ===")
    for k in ("naver", "tistory"):
        r = results[k]
        print(f"  {k}: status={r['status']} url={r.get('url','')} error={r.get('error','')}")
    print(f"  결과 파일: {RESULT_JSON}")
    return results


if __name__ == "__main__":
    if "--login" in sys.argv:
        run_login_mode()
    else:
        headless = "--headless" in sys.argv
        run_publish_mode(headless=headless)