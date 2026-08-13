import os
import re
import sys
import time
import json
import hashlib
import traceback
from pathlib import Path
from urllib.parse import urlparse, urljoin, quote

import requests
from playwright.sync_api import sync_playwright

APPS_SCRIPT_URL = os.environ["APPS_SCRIPT_WEBAPP_URL"].strip()
INBOUND_TOKEN = os.environ["INBOUND_TOKEN"].strip()
LATEST_LIMIT = int(os.getenv("LATEST_LIMIT", "200"))
RECOMMEND_LIMIT = int(os.getenv("RECOMMEND_LIMIT", "50"))

DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)

ALLOWED_NAVER_HOSTS = ("naver.com", "naver.me")

KNOWN_REVIEW_CARD_SELECTORS = [
    "ul#_review_list > li",
    "#_review_list > li",
    "li.EjjAW",
    "[data-review-id]",
]

KNOWN_AUTHOR_SELECTORS = [
    "span.pui__NMi-Dp",
    "span[class*='pui__NMi']",
    "[class*='pui__NMi']",
]

KNOWN_CONTENT_SELECTORS = [
    "div.pui__vn15t2 > a",
    "div[class*='pui__vn15t2'] > a",
    "[class*='pui__vn15t2'] a",
    "div[class*='pui__vn15t2']",
]

MORE_SELECTORS = [
    "div.NSTUp a.fvwqf",
    "a.fvwqf",
    "div.NSTUp a",
]

SORT_TEXT = {
    "LATEST": "최신순",
    "RECOMMEND": "추천순",
}


def post_api(payload):
    payload = dict(payload)
    payload["token"] = INBOUND_TOKEN
    last_error = None

    for attempt in range(1, 4):
        try:
            # Apps Script ContentService: POST -> one-time googleusercontent URL
            first = requests.post(
                APPS_SCRIPT_URL,
                json=payload,
                timeout=180,
                allow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0 NaverReviewCollector/7.0"},
            )

            if first.status_code in (301, 302, 303):
                location = first.headers.get("Location")
                if not location:
                    raise RuntimeError("Apps Script redirect has no Location header")

                response = requests.get(
                    location,
                    timeout=180,
                    allow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 NaverReviewCollector/7.0"},
                )
            elif first.status_code in (307, 308):
                location = first.headers.get("Location")
                if not location:
                    raise RuntimeError("Apps Script redirect has no Location header")
                response = requests.post(
                    location,
                    json=payload,
                    timeout=180,
                    allow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 NaverReviewCollector/7.0"},
                )
            else:
                response = first

            response.raise_for_status()

            try:
                data = response.json()
            except Exception:
                raise RuntimeError(
                    f"Apps Script returned non-JSON HTTP {response.status_code}: "
                    f"{response.text[:600]}"
                )

            if not data.get("ok"):
                raise RuntimeError(data.get("error") or "Apps Script API error")

            return data

        except Exception as exc:
            last_error = exc
            print(f"[Apps Script] retry {attempt}/3 failed: {exc}")
            if attempt < 3:
                time.sleep(2 * attempt)

    raise last_error


def allowed_naver_url(url):
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        return (
            p.scheme in ("http", "https")
            and any(host == h or host.endswith("." + h) for h in ALLOWED_NAVER_HOSTS)
        )
    except Exception:
        return False


def review_hash(author, content, visit_date):
    return hashlib.sha256(
        f"{author}|{content}|{visit_date}".encode("utf-8")
    ).hexdigest()


def safe_text(locator, timeout=1200):
    try:
        return locator.inner_text(timeout=timeout).strip()
    except Exception:
        return ""


def normalize_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def slug(text):
    return re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", text)[:50] or "place"


class NaverReviewCrawler:
    def start(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1440,1600",
                "--lang=ko-KR",
            ],
        )
        self.context = self.browser.new_context(
            viewport={"width": 1440, "height": 1600},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
        )

    def close(self):
        try:
            self.context.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self.pw.stop()
        except Exception:
            pass

    def save_debug(self, page, place_name, phase):
        name = f"{slug(place_name)}_{slug(phase)}"
        try:
            page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
        except Exception:
            pass
        try:
            (DEBUG_DIR / f"{name}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        try:
            frames = [{"name": f.name, "url": f.url} for f in page.frames]
            (DEBUG_DIR / f"{name}_frames.json").write_text(
                json.dumps(frames, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def expand_short_url(self, url):
        if "naver.me" not in url:
            return url
        try:
            r = requests.get(
                url,
                timeout=30,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            return r.url or url
        except Exception:
            return url

    def extract_place_id_from_string(self, text):
        patterns = [
            r"/place/(\d+)",
            r"/restaurant/(\d+)",
            r"/accommodation/(\d+)",
            r"[?&]placeId=(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, text or "")
            if m:
                return m.group(1)
        return ""

    def find_place_id_anywhere(self, page, place_name):
        # 1) current URL / frame URLs
        for candidate in [page.url] + [f.url for f in page.frames]:
            pid = self.extract_place_id_from_string(candidate)
            if pid:
                return pid

        # 2) all hrefs in all frames
        for frame in page.frames:
            try:
                hrefs = frame.locator("a[href]").evaluate_all(
                    "els => els.map(e => ({href:e.href||'', text:(e.innerText||'').trim()}))"
                )
            except Exception:
                continue

            # name-matching link first
            for item in hrefs:
                if place_name and place_name in (item.get("text") or ""):
                    pid = self.extract_place_id_from_string(item.get("href") or "")
                    if pid:
                        return pid

            # first place/detail href fallback
            for item in hrefs:
                pid = self.extract_place_id_from_string(item.get("href") or "")
                if pid:
                    return pid

        return ""

    def resolve_place_id(self, place_name, naver_url):
        url = self.expand_short_url(naver_url)
        pid = self.extract_place_id_from_string(url)
        if pid:
            return pid

        page = self.context.new_page()
        try:
            print("Resolving place ID:", place_name, url)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)

            pid = self.find_place_id_anywhere(page, place_name)
            if pid:
                print("Resolved place ID:", pid)
                return pid

            # Search result pages sometimes load results only after scroll/wait
            for _ in range(3):
                for frame in page.frames:
                    try:
                        frame.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    except Exception:
                        pass
                page.wait_for_timeout(1800)

                pid = self.find_place_id_anywhere(page, place_name)
                if pid:
                    print("Resolved place ID after wait:", pid)
                    return pid

            self.save_debug(page, place_name, "resolve_place_id_failed")
            raise RuntimeError(
                f"검색 결과에서 Place ID를 찾지 못했습니다. PLACE_NAME='{place_name}'"
            )
        finally:
            page.close()

    def direct_review_url(self, place_id):
        return f"https://pcmap.place.naver.com/place/{place_id}/review/visitor"

    def wait_for_review_page(self, page, place_name):
        # Current review page may need several seconds of client rendering.
        for round_no in range(10):
            body = normalize_text(safe_text(page.locator("body"), timeout=2500))

            if (
                "추천순" in body
                or "최신순" in body
                or "리뷰 제공 기준" in body
                or "사진/영상 리뷰만" in body
            ):
                return True

            page.wait_for_timeout(1200)

        self.save_debug(page, place_name, "review_page_not_ready")
        return False

    def click_sort(self, page, sort_type):
        wanted = SORT_TEXT[sort_type]

        # Text-based selectors are more stable than minified CSS class names.
        selectors = [
            page.get_by_text(wanted, exact=True),
            page.get_by_role("link", name=re.compile(wanted)),
            page.get_by_role("button", name=re.compile(wanted)),
        ]

        for loc in selectors:
            try:
                for i in range(min(loc.count(), 20)):
                    item = loc.nth(i)
                    if item.is_visible():
                        item.click(timeout=4000)
                        page.wait_for_timeout(1800)
                        return True
            except Exception:
                pass

        # JS fallback: click visible element whose trimmed text is exactly wanted.
        try:
            clicked = page.evaluate(
                """wanted => {
                  const els = [...document.querySelectorAll('a,button,span')];
                  const el = els.find(e => (e.innerText||'').trim() === wanted &&
                    e.getClientRects().length);
                  if (!el) return false;
                  el.click();
                  return true;
                }""",
                wanted,
            )
            if clicked:
                page.wait_for_timeout(1800)
                return True
        except Exception:
            pass

        print("WARN sort not clicked:", wanted)
        return False

    def known_cards(self, page):
        for selector in KNOWN_REVIEW_CARD_SELECTORS:
            try:
                loc = page.locator(selector)
                if loc.count() >= 1:
                    return loc
            except Exception:
                pass
        return None

    def generic_cards(self, page):
        # Find likely review cards without depending on Naver's minified class names.
        # Review cards are commonly LI elements containing time/profile/follow/review text.
        try:
            candidates = page.locator("li")
            accepted = []

            for i in range(min(candidates.count(), 500)):
                li = candidates.nth(i)
                try:
                    text = normalize_text(safe_text(li, timeout=500))
                    if len(text) < 15:
                        continue
                    if len(text) > 5000:
                        continue

                    has_time = li.locator("time").count() > 0
                    has_profile_signal = (
                        "팔로우" in text
                        or "리뷰" in text
                        or has_time
                    )

                    # Avoid nav/menu list items.
                    if has_profile_signal and not (
                        text in ("소식", "예약", "전시", "리뷰", "사진", "정보")
                    ):
                        accepted.append(li)
                except Exception:
                    continue

            return accepted
        except Exception:
            return []

    def extract_known(self, elem, rank):
        author = ""
        for s in KNOWN_AUTHOR_SELECTORS:
            try:
                author = safe_text(elem.locator(s).first)
                if author:
                    break
            except Exception:
                pass

        content = ""
        for s in KNOWN_CONTENT_SELECTORS:
            try:
                content = safe_text(elem.locator(s).first)
                if content:
                    break
            except Exception:
                pass

        return self.finish_extract(elem, rank, author, content)

    def extract_generic(self, elem, rank):
        raw = safe_text(elem)
        lines = [
            normalize_text(x)
            for x in raw.splitlines()
            if normalize_text(x)
        ]

        # Author: first short non-meta line.
        author = "익명"
        for line in lines[:8]:
            if 1 <= len(line) <= 30 and not any(
                key in line
                for key in [
                    "팔로우", "리뷰", "사진", "방문", "이용",
                    "예약", "도움", "메뉴", "더보기",
                ]
            ):
                author = line
                break

        # Content: choose longest meaningful line excluding common metadata.
        meta_words = [
            "팔로우", "리뷰", "사진", "방문일", "이용일",
            "예약", "더보기", "도움이 돼요", "개의 리뷰",
        ]
        content_lines = [
            line for line in lines
            if len(line) >= 8
            and not any(word in line for word in meta_words)
            and line != author
        ]
        content = max(content_lines, key=len) if content_lines else ""

        return self.finish_extract(elem, rank, author, content)

    def finish_extract(self, elem, rank, author, content):
        time_texts = []
        try:
            times = elem.locator("time")
            for i in range(min(times.count(), 6)):
                txt = normalize_text(safe_text(times.nth(i)))
                if txt:
                    time_texts.append(txt)
        except Exception:
            pass

        visit_date = time_texts[0] if time_texts else ""
        review_date = time_texts[-1] if time_texts else ""

        photo_count = 0
        try:
            # exclude tiny/profile icons as much as possible by looking for media links/containers
            photo_count = elem.locator(
                "a[href*='photo'] img, [class*='photo'] img, [class*='Photo'] img"
            ).count()
        except Exception:
            pass

        author = normalize_text(author) or "익명"
        content = normalize_text(content)
        rid = review_hash(author, content, visit_date)

        return {
            "id": rid,
            "reviewId": rid,
            "author": author,
            "content": content,
            "visitDate": visit_date,
            "reviewDate": review_date,
            "photoCount": photo_count,
            "rank": rank,
        }

    def click_more(self, page):
        for selector in MORE_SELECTORS:
            try:
                loc = page.locator(selector)
                for i in range(loc.count() - 1, -1, -1):
                    item = loc.nth(i)
                    if item.is_visible():
                        item.scroll_into_view_if_needed()
                        item.click(timeout=3000)
                        page.wait_for_timeout(1600)
                        return True
            except Exception:
                pass

        # Stable text fallback.
        try:
            loc = page.get_by_text("더보기", exact=True)
            for i in range(loc.count() - 1, -1, -1):
                item = loc.nth(i)
                if item.is_visible():
                    item.scroll_into_view_if_needed()
                    item.click(timeout=3000)
                    page.wait_for_timeout(1600)
                    return True
        except Exception:
            pass

        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
        except Exception:
            pass

        return False

    def collect_once(self, place_name, place_id, sort_type, limit, existing_ids):
        page = self.context.new_page()

        try:
            review_url = self.direct_review_url(place_id)
            print(f"OPEN DIRECT REVIEW: {review_url}")
            page.goto(review_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)

            if not self.wait_for_review_page(page, place_name):
                raise RuntimeError(
                    "리뷰 페이지 텍스트가 준비되지 않았습니다. "
                    f"target={page.url}"
                )

            self.click_sort(page, sort_type)

            results = []
            seen = set()
            existing_ids = set(str(x) for x in (existing_ids or []))
            stagnant = 0

            for loop in range(70):
                known = self.known_cards(page)

                if known is not None:
                    elements = [known.nth(i) for i in range(known.count())]
                    mode = "known"
                else:
                    elements = self.generic_cards(page)
                    mode = "generic"

                print(
                    f"{sort_type} loop={loop+1} mode={mode} "
                    f"cards={len(elements)} collected={len(results)}"
                )

                before = len(results)
                stop = False

                for elem in elements:
                    if len(results) >= limit:
                        stop = True
                        break

                    try:
                        review = (
                            self.extract_known(elem, len(results) + 1)
                            if mode == "known"
                            else self.extract_generic(elem, len(results) + 1)
                        )
                    except Exception:
                        continue

                    if not review["content"]:
                        continue

                    rid = review["id"]

                    if sort_type == "LATEST" and rid in existing_ids:
                        stop = True
                        break

                    if rid in seen:
                        continue

                    seen.add(rid)
                    results.append(review)

                if stop or len(results) >= limit:
                    break

                stagnant = stagnant + 1 if len(results) == before else 0

                if stagnant >= 3:
                    break

                self.click_more(page)

            if not results:
                self.save_debug(page, place_name, f"{sort_type}_zero_reviews")
                raise RuntimeError(
                    "리뷰 페이지는 열렸지만 리뷰 카드 추출 결과가 0개입니다. "
                    f"target={page.url}"
                )

            return results[:limit]

        finally:
            page.close()

    def collect(self, place_name, naver_url, sort_type, limit, existing_ids):
        # Resolve once per collection. In practice direct place IDs are stable.
        place_id = self.resolve_place_id(place_name, naver_url)

        last_error = None
        for attempt in range(1, 4):
            try:
                print(f"{sort_type} attempt {attempt}/3 place_id={place_id}")
                return self.collect_once(
                    place_name,
                    place_id,
                    sort_type,
                    limit,
                    existing_ids,
                )
            except Exception as exc:
                last_error = exc
                print(f"{sort_type} attempt {attempt} failed: {exc}")
                if attempt < 3:
                    time.sleep(3 * attempt)

        raise last_error


def push_error(place_name, sort_type, exc):
    try:
        post_api({
            "action": "push_error",
            "placeName": place_name,
            "sort": sort_type,
            "message": f"{type(exc).__name__}: {exc}",
        })
    except Exception as nested:
        print("Could not push error to Apps Script:", nested)


def main():
    data = post_api({"action": "get_places"})
    places = data.get("places", [])

    if not places:
        print("No enabled places.")
        return 0

    print("Enabled places:", len(places))

    crawler = NaverReviewCrawler()
    crawler.start()

    successes = 0
    failures = 0

    try:
        for place in places:
            name = place["name"]
            url = place["naverUrl"]
            existing_ids = place.get("existingIds", [])

            print("\n====================================")
            print("PLACE:", name)
            print("====================================")

            try:
                latest = crawler.collect(
                    name, url, "LATEST", LATEST_LIMIT, existing_ids
                )
                result = post_api({
                    "action": "push_reviews",
                    "placeName": name,
                    "sort": "LATEST",
                    "reviews": latest,
                })
                print("LATEST saved:", result)
                successes += 1
            except Exception as exc:
                failures += 1
                print("LATEST ERROR:", exc)
                traceback.print_exc()
                push_error(name, "LATEST", exc)

            time.sleep(2)

            try:
                recommend = crawler.collect(
                    name, url, "RECOMMEND", RECOMMEND_LIMIT, []
                )
                result = post_api({
                    "action": "push_reviews",
                    "placeName": name,
                    "sort": "RECOMMEND",
                    "reviews": recommend,
                })
                print("RECOMMEND saved:", result)
                successes += 1
            except Exception as exc:
                failures += 1
                print("RECOMMEND ERROR:", exc)
                traceback.print_exc()
                push_error(name, "RECOMMEND", exc)

            time.sleep(2)

    finally:
        crawler.close()

    print("\nRUN SUMMARY")
    print("successes =", successes)
    print("failures  =", failures)

    # Partial success is kept green; FETCH_LOG is the source of per-place status.
    return 0 if successes > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
