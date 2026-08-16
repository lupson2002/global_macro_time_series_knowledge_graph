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
import re
import datetime as _dt
from pathlib import Path
from playwright.sync_api import sync_playwright

DRAFT = Path(__file__).parent / "tistory_draft.md"
USER_DATA_DIR = "/home/mikey/browser_user_data"
LOG_DIR = Path(__file__).parent / "logs"
RESULT_JSON = LOG_DIR / "blog_publish_result.json"
# 세션 쿠키 영속화 — Chrome 세션 쿠키(NID_AUT/NID_SES)는 프로필에 안 남아
# storage_state JSON으로 명시 저장·복원한다 (--login 1회 후 재로그인 불필요).
COOKIES_JSON = LOG_DIR / "blog_cookies.json"

NAVER_WRITE = "https://blog.naver.com/lupson2002/postwrite"
TISTORY_WRITE = "https://tmmm0123.tistory.com/manage/post"

STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]

LOGIN_HINTS = ("login", "signin", "account", "auth/login")

# 마크다운 이미지 참조: ![캡션](경로) + 뒤따르는 *캡션* 이탤릭 줄(차트 섹션 형식)
_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)(?:\n\*[^*]*\*)?")


def _clipboard_copy(page, text: str) -> None:
    """Playwright 브라우저 컨텍스트 내 클립보드에 텍스트 복사.
    pyperclip 의존 제거 — OS 클립보드 대신 Clipboard API 사용.
    headless/cron 환경에서도 동작 (DISPLAY 불필요).
    """
    page.evaluate(f"navigator.clipboard.writeText({json.dumps(text)})")


def load_draft(draft_path: str | None = None):
    p = Path(draft_path) if draft_path else DRAFT
    raw = p.read_text(encoding="utf-8").strip()
    lines = raw.split("\n")
    title = lines[0].replace("#", "").strip()
    body = "\n".join(lines[1:]).strip()
    return title, body


def extract_images(body: str) -> tuple[str, list[str]]:
    """본문에서 ![캡션](경로) 이미지 참조 추출 (뒤따르는 *캡션* 줄 포함).

    반환: (이미지 참조 제거된 본문, 존재하는 이미지 절대경로 리스트)
    파일이 실제로 존재하는 참조만 제거·업로드 대상으로 삼고, 없는 참조는 원문 유지.
    캡션 줄도 함께 제거해 발행물에서 캡션-이미지 순서가 깨지지 않게 한다.
    """
    images: list[str] = []

    def _repl(m: re.Match) -> str:
        path = m.group(1).strip()
        if path and Path(path).exists():
            images.append(path)
            return ""  # 업로드로 대체 → 본문에서 제거 (캡션 줄 포함)
        return m.group(0)  # 파일 없으면 마크다운 원문 유지

    new_body = _IMG_RE.sub(_repl, body)
    return new_body, images


def upload_images(page, images: list[str], platform: str) -> list[str]:
    """에디터 이미지 업로드 — file chooser 이벤트 캡처 방식.

    네이버/티스토리 모두 툴바 버튼 클릭 시 OS 파일 선택(file chooser)이 열린다.
    Playwright expect_file_chooser 로 캡처해 set_files 로 파일을 설정한다.
    (input[type='file'] 은 티스토리에서 DOM 에 없어 set_input_files 가 타임아웃됨 — 실측)

    텍스트 붙여넣기 후 호출 → 커서 위치(본문 끝)에 이미지가 삽입된다.
    각 이미지 업로드 후 에디터 반영을 위해 대기.
    실패한 이미지 경로 리스트를 반환 (결과 JSON에 기록 — 조용한 유실 방지).
    """
    failed: list[str] = []
    for img in images:
        name = Path(img).name
        try:
            if platform == "네이버":
                # 작성 중 글 팝업 등이 이미지 버튼 클릭을 가로막으면 먼저 닫기
                try:
                    if page.locator(".se-popup-alert-confirm").count() > 0:
                        page.locator(".se-popup-alert-confirm button").first.click(timeout=3000)
                        page.wait_for_timeout(1000)
                except Exception:
                    pass
                # 사진 추가 버튼 → file chooser 캡처
                with page.expect_file_chooser(timeout=15000) as fc_info:
                    page.click("button[data-name='image']", timeout=5000)
                fc = fc_info.value
                fc.set_files(img)
            elif platform == "티스토리":
                # 첨부 버튼(visible) → 사진 메뉴 → file chooser 캡처
                page.locator("[aria-label='첨부']:visible").first.click(timeout=5000)
                page.wait_for_timeout(1500)
                with page.expect_file_chooser(timeout=15000) as fc_info:
                    page.locator(".mce-menu-item:has-text('사진')").first.click(timeout=5000)
                fc = fc_info.value
                fc.set_files(img)
            else:
                raise RuntimeError(f"unknown platform: {platform}")
            page.wait_for_timeout(4000)  # 업로드 + 에디터 반영 대기
            print(f"[{platform}] 이미지 업로드: {name}")
        except Exception as e:
            print(f"[{platform}] 이미지 업로드 실패 ({name}): {e}")
            failed.append(img)
    return failed


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
    u = url.lower()
    # 발행 성공: 글 목록(manage/posts/) 또는 게시글(/entry/ 또는 tistory.com/<id>)
    # 주의: "/manage/posts" 를 "/manage/post" 보다 먼저 검사(부분 문자열 포함 관계).
    if "/manage/posts" in u or "/entry/" in u:
        return {"status": "OK", "url": url, "title": "", "error": ""}
    # 글쓰기 페이지(manage/post, manage/newpost)에 그대로 머물면 발행 실패
    if "/manage/post" in u or "/manage/newpost" in u:
        return {"status": "FAIL", "url": url, "title": "", "error": "still_on_write_page"}
    if "tistory.com/" in u:
        return {"status": "OK", "url": url, "title": "", "error": ""}
    return {"status": "UNKNOWN", "url": url, "title": "", "error": "url_ambiguous"}


def wait_login(page, label):
    """로그인 페이지 감지 시 True 반환(세션 만료). 호출자가 early-return 해야 함."""
    time.sleep(3)
    if _is_login_url(page.url):
        print(f"[{label}] ⚠️ 로그인 페이지 감지 — 세션 만료. 발행 중단(--login 재로그인 필요).")
        return True
    return False


def publish_naver(page, title, body, images=None):
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

        title_filled = False
        for sel in [".se-documentTitle", ".se-text-paragraph:first-child"]:
            try:
                page.click(sel, timeout=5000)
                page.keyboard.type(title, delay=20)
                title_filled = True
                break
            except Exception:
                continue
        if not title_filled:
            print("[네이버] ⚠️ 제목 입력 셀렉터 매칭 실패 — 기본 포커스 진입 시도")

        body_pasted = False
        _clipboard_copy(page, body)
        for sel in [".se-section-text", "[contenteditable]"]:
            try:
                page.click(sel, timeout=5000)
                page.wait_for_timeout(300)
                page.keyboard.press("Control+V")
                body_pasted = True
                break
            except Exception:
                continue
        if not body_pasted:
            print("[네이버] ⚠️ 본문 영역 클릭 실패 — 클립보드 붙여넣기 미완료 가능성")
        page.wait_for_timeout(1500)

        # 👑 이미지 업로드 — 텍스트 붙여넣기 후 커서 위치(본문 끝)에 삽입
        failed_imgs: list[str] = []
        if images:
            failed_imgs = upload_images(page, images, "네이버")

        published_clicked = False
        for sel in ["button[class*='publish' i]", "[class*='publish' i] button"]:
            try:
                page.click(sel, timeout=5000)
                published_clicked = True
                break
            except Exception:
                continue
        if not published_clicked:
            print("[네이버] ⚠️ 1차 발행 버튼 클릭 실패")
        page.wait_for_timeout(2000)
        confirm_clicked = False
        for sel in ["button[class*='confirm' i]", "[class*='confirm' i] button", "button[class*='publish' i]"]:
            try:
                page.click(sel, timeout=5000)
                confirm_clicked = True
                break
            except Exception:
                continue
        if not confirm_clicked:
            print("[네이버] ⚠️ 최종 발행 확인 버튼 클릭 실패")
        page.wait_for_timeout(4000)
        print(f"[네이버] 발행 시도 완료 → {page.url}")
        result = _verify_naver(page)
        if failed_imgs:
            result["image_upload_failed"] = [Path(i).name for i in failed_imgs]
        return result
    except Exception as e:
        shot = _save_screenshot(page, "naver")
        return {"status": "FAIL", "url": page.url if page else "", "title": "", "error": f"{type(e).__name__}: {e}", "screenshot": shot}


def publish_tistory(page, title, body, images=None):
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

        # 제목 — 티스토리 신규 에디터: textarea.textarea_tit (실측, #title 등은 없음)
        try:
            page.fill("textarea.textarea_tit", title, timeout=5000)
        except Exception as e:
            print(f"[티스토리] 제목 입력 실패: {e}")

        # 본문 — TinyMCE iframe contenteditable 클릭 후 page.keyboard 입력 (실측)
        try:
            frame = None
            for f in page.frames:
                try:
                    if f.locator("[contenteditable='true']").count() > 0:
                        frame = f
                        break
                except Exception:
                    continue
            if frame is None:
                frame = page.frames[1]  # 폴백
            ed = frame.locator("[contenteditable='true']").first
            ed.click(timeout=5000)
            page.keyboard.type(body, delay=0)
            print(f"[티스토리] 본문 입력 OK ({len(body)}자)")
        except Exception as e:
            print(f"[티스토리] 본문 입력 실패: {e}")
        page.wait_for_timeout(1500)

        # 👑 이미지 업로드 — 텍스트 입력 후 커서 위치(본문 끝)에 삽입
        failed_imgs: list[str] = []
        if images:
            failed_imgs = upload_images(page, images, "티스토리")

        # 발행 — '완료' → 발행정보 모달 → '공개' 라디오 → '공개 발행' (실측)
        try:
            page.locator("button.btn.btn-default:has-text('완료')").first.click(timeout=5000)
            page.wait_for_timeout(3000)
            page.locator("input[name='basicSet'][value='20']").check(timeout=5000)
            page.wait_for_timeout(1000)
            page.locator("button.btn.btn-default:has-text('공개 발행')").first.click(timeout=5000)
            print("[티스토리] '공개 발행' 클릭")
        except Exception as e:
            print(f"[티스토리] 발행 버튼 클릭 실패: {e}")
        page.wait_for_timeout(6000)
        print(f"[티스토리] 발행 시도 완료 → {page.url}")
        result = _verify_tistory(page)
        if failed_imgs:
            result["image_upload_failed"] = [Path(i).name for i in failed_imgs]
        return result
    except Exception as e:
        shot = _save_screenshot(page, "tistory")
        return {"status": "FAIL", "url": page.url if page else "", "title": "", "error": f"{type(e).__name__}: {e}", "screenshot": shot}


def _wait_login_done(page, label: str, timeout_s: int = 300) -> bool:
    """URL이 로그인 페이지에서 벗어날 때까지 폴링 — 로그인 완료 자동 감지.

    input() 대신 사용 → 백그라운드 실행 가능 (사용자는 브라우저 창에서 로그인만).
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _is_login_url(page.url):
            print(f"[{label}] ✅ 로그인 감지 완료 → {page.url[:60]}")
            return True
        time.sleep(2)
    print(f"[{label}] ⚠️ 로그인 감지 실패 ({timeout_s}초 초과) — 마지막 URL: {page.url[:60]}")
    return False


def _verify_persistent_session(cookies_json: Path) -> None:
    """로그인 후 저장된 쿠키에 장기(7일+) 세션 쿠키가 있는지 검증.

    네이버(NID_AUT) / 카카오(SESS 등) '자동 로그인' 체크 여부가 핵심 —
    미체크 시 세션 쿠키가 세션 전용으로 저장되어 수시간~1일 만에 만료된다.
    장기 쿠키가 없으면 경고를 출력한다 (발행 자동화가 일시에 무너지는 것 방지).
    """
    try:
        state = json.loads(Path(cookies_json).read_text(encoding="utf-8"))
        now = time.time()
        persistent = [c for c in state.get("cookies", [])
                      if (c.get("expires") or 0) - now > 7 * 86400]
        if not persistent:
            print("⚠️ 경고: 7일 이상 지속되는 세션 쿠키가 없습니다.")
            print("   네이버/카카오 로그인 화면에서 '자동 로그인'을 반드시 체크하세요.")
            print("   미체크 시 세션이 수시간~1일 만에 만료되어 발행 자동화가 실패합니다.")
        else:
            doms = sorted({c.get("domain", "") for c in persistent})
            print(f"✅ 장기 세션 쿠키 {len(persistent)}개 감지 (30일 유지 가능): {doms[:4]}")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 세션 쿠키 검증 실패: {e}")


def run_login_mode():
    print("=== 로그인 세션 저장 모드 (자동 감지) ===")
    print("브라우저가 열리면 각 사이트에 직접 로그인하세요. 완료를 자동 감지합니다.")
    print("⚠️ 네이버/카카오 로그인 시 '자동 로그인' 체크 필수 — 세션 30일 유지 (미체크 시 단기 세션으로 만료).")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR, headless=False, args=STEALTH_ARGS,
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = ctx.new_page()
        page.goto("https://nid.naver.com/nidlogin.login")
        print("[1/2] 네이버 로그인 대기 중... (로그인 완료 자동 감지)")
        _wait_login_done(page, "네이버")
        page.goto(TISTORY_WRITE)
        print("[2/2] 티스토리(카카오) 로그인 대기 중...")
        _wait_login_done(page, "티스토리")
        # 세션 쿠키를 JSON으로 명시 저장 — Chrome 프로필 영속 실패를 우회.
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(COOKIES_JSON))
        print(f"💾 세션 쿠키 저장: {COOKIES_JSON}")
        # 👑 [FIX] 30일 지속 세션 검증 — '자동 로그인' 체크 여부 확인.
        # 카카오/네이버 자동로그인 미체크 시 단기 세션(수시간~1일)으로 금방 만료됨.
        _verify_persistent_session(COOKIES_JSON)
        ctx.close()
    print("✅ 세션 저장 완료:", USER_DATA_DIR)


def run_publish_mode(headless: bool, draft_path: str | None = None, result_json: str | None = None,
                     platforms: tuple[str, ...] = ("naver", "tistory")):
    draft_path = draft_path or str(DRAFT)
    result_json = result_json or str(RESULT_JSON)
    title, body = load_draft(draft_path)
    body, images = extract_images(body)
    print(f"원고: 제목='{title}' / 본문 {len(body)}자 / 이미지 {len(images)}장 / headless={headless} / platforms={platforms}")
    results = {}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR, headless=headless, args=STEALTH_ARGS,
            permissions=["clipboard-read", "clipboard-write"],
        )
        # 저장된 세션 쿠키 복원 (--login 1회 후 재로그인 불필요)
        if COOKIES_JSON.exists():
            try:
                state = json.loads(COOKIES_JSON.read_text(encoding="utf-8"))
                cookies = state.get("cookies", [])
                if cookies:
                    ctx.add_cookies(cookies)
                    print(f"🍪 세션 쿠키 복원: {len(cookies)}개")
            except Exception as e:
                print(f"[WARN] 쿠키 복원 실패({e}) — --login 재실행 필요할 수 있음.")
        page = ctx.new_page()
        if "naver" in platforms:
            results["naver"] = publish_naver(page, title, body, images)
        if "tistory" in platforms:
            results["tistory"] = publish_tistory(page, title, body, images)
        # FAIL 시 스크린샷이 아직 없으면 한 번 더
        for plat in platforms:
            if plat in results and results[plat].get("status") == "FAIL" and not results[plat].get("screenshot"):
                results[plat]["screenshot"] = _save_screenshot(page, plat)
        # 👑 [FIX] 세션 쿠키 갱신 — 발행 후 최신 쿠키 저장 (세션 로테이션/갱신 대응).
        # 단, FAIL(로그인 페이지 리다이렉트)이 있으면 재저장 생략 — 만료 컨텍스트로
        # blog_cookies.json 을 덮어쓰면 유효했던 네이버/티스토리 쿠키까지 파괴됨.
        if all(results.get(p, {}).get("status") != "FAIL" for p in platforms):
            try:
                ctx.storage_state(path=str(COOKIES_JSON))
                print("💾 발행 후 세션 쿠키 재저장 완료")
            except Exception as e:
                print(f"[WARN] 발행 후 쿠키 재저장 실패: {e}")
        else:
            print("[WARN] 발행 실패 플랫폼 존재 — 쿠키 재저장 생략 (마지막 양호 쿠키 보존)")
        time.sleep(3)
        ctx.close()

    # 제목 채우기
    for plat in platforms:
        if plat in results:
            results[plat]["title"] = title
    results["_meta"] = {"published_at": _dt.datetime.now().isoformat(timespec="seconds")}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    Path(result_json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 게시 결과 ===")
    for k in platforms:
        if k in results:
            r = results[k]
            print(f"  {k}: status={r['status']} url={r.get('url','')} error={r.get('error','')}")
    print(f"  결과 파일: {result_json}")
    return results


if __name__ == "__main__":
    import argparse as _ap
    _ap_ = _ap.ArgumentParser(description="블로그 발행 (네이버 + 티스토리)")
    _ap_.add_argument("--login", action="store_true", help="로그인 세션 저장 모드")
    _ap_.add_argument("--headless", action="store_true", help="헤드리스 모드")
    _ap_.add_argument("--draft", default=None, help="원고 파일 경로 (기본 tistory_draft.md)")
    _ap_.add_argument("--result-json", default=None, help="결과 JSON 경로")
    _ap_.add_argument("--platform", default="both", choices=["naver", "tistory", "both"],
                      help="발행 플랫폼 (기본 both — 재발행 시 단일 플랫폼 선택 가능)")
    _args = _ap_.parse_args()
    if _args.login:
        run_login_mode()
    else:
        platforms = ("naver", "tistory") if _args.platform == "both" else (_args.platform,)
        run_publish_mode(headless=_args.headless, draft_path=_args.draft,
                         result_json=_args.result_json, platforms=platforms)