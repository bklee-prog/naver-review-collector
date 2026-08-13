import os
import re
import sys
import time
import hashlib
import traceback
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

APPS_SCRIPT_URL = os.environ["APPS_SCRIPT_WEBAPP_URL"].strip()
INBOUND_TOKEN = os.environ["INBOUND_TOKEN"].strip()

LATEST_LIMIT = int(os.getenv("LATEST_LIMIT", "200"))
RECOMMEND_LIMIT = int(os.getenv("RECOMMEND_LIMIT", "50"))

ALLOWED_NAVER_HOSTS = ("naver.com", "naver.me")

REVIEW_LIST_SELECTORS = [
    "ul#_review_list > li.EjjAW",
    "ul#_review_list > li",
    "#_review_list > li",
]

AUTHOR_SELECTORS = [
    "span.pui__NMi-Dp",
    "span[class*='pui__NMi']",
    "[class*='pui__NMi']",
]

CONTENT_SELECTORS = [
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

SORT_SELECTORS = [
    "a.place_btn_option",
    "[class*='place_btn_option']",
]


def post_api(payload):
    payload = dict(payload)
    payload["token"] = INBOUND_TOKEN

    r = requests.post(
        APPS_SCRIPT_URL,
        json=payload,
        timeout=180,
        allow_redirects=True,
    )
    r.raise_for_status()

    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"Apps Script returned non-JSON: {r.text[:800]}")

    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "Apps Script API error")

    return data


def allowed_naver_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        if parsed.scheme not in ("http", "https"):
            return False

        return any(host == h or host.endswith("." + h) for h in ALLOWED_NAVER_HOSTS)
    except Exception:
        return False


def hash_review(author, content, visit_date):
    raw = f"{author}|{content}|{visit_date}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def safe_text(locator):
    try:
        return locator.inner_text(timeout=1200).strip()
    except Exception:
        return ""


def first_text(scope, selectors):
    for selector in selectors:
        try:
            loc = scope.locator(selector).first
            if loc.count():
                text = safe_text(loc)
                if text:
                    return text
        except Exception:
            pass

    return ""


class NaverReviewCrawler:
    def __init__(self):
        self.browser = None
        self.context = None

    def start(self):
        self.pw = sync_playwright().start()

        self.browser = self.pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1440,1200",
                "--lang=ko-KR",
            ],
        )

        self.context = self.browser.new_context(
            viewport={"width": 1440, "height": 1200},
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
            if self.context:
                self.context.close()
        finally:
            try:
                if self.browser:
                    self.browser.close()
            finally:
                try:
                    self.pw.stop()
                except Exception:
                    pass

    def find_entry_frame(self, page):
        for _ in range(30):
            for frame in page.frames:
                name = frame.name or ""
                url = frame.url or ""

                if name == "entryIframe":
                    return frame

                if "place.naver.com" in url and (
                    "/restaurant/" in url
                    or "/place/" in url
                    or "/accommodation/" in url
                    or "/hospital/" in url
                    or "/beauty/" in url
                ):
                    return frame

            page.wait_for_timeout(350)

        return None

    def open_review_area(self, page, naver_url):
        if not allowed_naver_url(naver_url):
            raise RuntimeError("PLACES의 NAVER_URL은 naver.com/naver.me 주소여야 합니다.")

        print("OPEN:", naver_url)

        page.goto(
            naver_url,
            wait_until="domcontentloaded",
            timeout=45000,
        )

        page.wait_for_timeout(3000)

        frame = self.find_entry_frame(page)
        target = frame if frame else page

        if "/review" not in (target.url or ""):
            clicked = False

            candidates = [
                target.get_by_text("리뷰", exact=True),
                target.get_by_role("link", name=re.compile(r"^리뷰")),
                target.get_by_role("button", name=re.compile(r"^리뷰")),
            ]

            for loc in candidates:
                try:
                    for i in range(min(loc.count(), 8)):
                        item = loc.nth(i)
                        if item.is_visible():
                            item.click(timeout=3000)
                            clicked = True
                            break

                    if clicked:
                        break
                except Exception:
                    pass

            if clicked:
                page.wait_for_timeout(2500)

                new_frame = self.find_entry_frame(page)
                if new_frame:
                    target = new_frame

        found = False

        for selector in REVIEW_LIST_SELECTORS:
            try:
                target.locator(selector).first.wait_for(
                    state="attached",
                    timeout=7000,
                )
                found = True
                break
            except Exception:
                pass

        if not found:
            try:
                target.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                target.wait_for_timeout(1800)
            except Exception:
                pass

            for selector in REVIEW_LIST_SELECTORS:
                try:
                    if target.locator(selector).count():
                        found = True
                        break
                except Exception:
                    pass

        if not found:
            raise RuntimeError(
                "리뷰 목록 DOM을 찾지 못했습니다. "
                "네이버 페이지 구조가 변경되었거나 해당 URL이 장소 상세 페이지로 열리지 않았습니다. "
                f"target={target.url}"
            )

        return target

    def click_sort(self, target, sort_type):
        wanted = "최신순" if sort_type == "LATEST" else "추천순"

        for selector in SORT_SELECTORS:
            try:
                loc = target.locator(selector)

                for i in range(min(loc.count(), 15)):
                    item = loc.nth(i)

                    if wanted in safe_text(item) and item.is_visible():
                        item.click(timeout=3000)
                        target.wait_for_timeout(1600)
                        return True
            except Exception:
                pass

        candidates = [
            target.get_by_text(wanted, exact=True),
            target.get_by_role("link", name=re.compile(wanted)),
            target.get_by_role("button", name=re.compile(wanted)),
        ]

        for loc in candidates:
            try:
                for i in range(min(loc.count(), 10)):
                    item = loc.nth(i)

                    if item.is_visible():
                        item.click(timeout=3000)
                        target.wait_for_timeout(1600)
                        return True
            except Exception:
                pass

        print("WARN: sort button not found:", wanted)
        return False

    def get_review_elements(self, target):
        for selector in REVIEW_LIST_SELECTORS:
            try:
                loc = target.locator(selector)
                if loc.count():
                    return loc
            except Exception:
                pass

        return target.locator("ul#_review_list > li")

    def extract_review(self, elem, rank):
        author = first_text(elem, AUTHOR_SELECTORS) or "익명"
        content = first_text(elem, CONTENT_SELECTORS)

        time_texts = []

        try:
            times = elem.locator("time")

            for i in range(min(times.count(), 5)):
                txt = safe_text(times.nth(i))
                if txt:
                    time_texts.append(txt)
        except Exception:
            pass

        visit_date = time_texts[0] if time_texts else ""
        review_date = time_texts[-1] if len(time_texts) > 1 else visit_date

        photo_count = 0

        # 네이버 DOM 구조가 바뀔 수 있어 사진 수는 보수적으로 수집.
        try:
            images = elem.locator(
                "a[href*='photo'], [class*='photo'] img, [class*='Photo'] img"
            )
            photo_count = images.count()
        except Exception:
            pass

        rid = hash_review(author, content, visit_date)

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

    def click_more(self, target):
        for selector in MORE_SELECTORS:
            try:
                loc = target.locator(selector)

                for i in range(loc.count() - 1, -1, -1):
                    item = loc.nth(i)

                    if item.is_visible():
                        item.scroll_into_view_if_needed()
                        item.click(timeout=3000)
                        target.wait_for_timeout(1500)
                        return True
            except Exception:
                pass

        try:
            loc = target.get_by_text("더보기", exact=True)

            for i in range(loc.count() - 1, -1, -1):
                item = loc.nth(i)

                try:
                    if item.is_visible():
                        item.scroll_into_view_if_needed()
                        item.click(timeout=2500)
                        target.wait_for_timeout(1500)
                        return True
                except Exception:
                    pass
        except Exception:
            pass

        try:
            target.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            target.wait_for_timeout(1200)
        except Exception:
            pass

        return False

    def collect(self, naver_url, sort_type, limit, existing_ids):
        sort_type = sort_type.upper()
        limit = max(1, min(int(limit), 500))
        existing_ids = set(str(x) for x in (existing_ids or []))

        page = self.context.new_page()

        try:
            target = self.open_review_area(page, naver_url)
            self.click_sort(target, sort_type)

            results = []
            seen = set()
            stagnant = 0

            for _ in range(70):
                elems = self.get_review_elements(target)
                count = elems.count()
                before = len(results)
                stop = False

                for i in range(count):
                    if len(results) >= limit:
                        stop = True
                        break

                    try:
                        review = self.extract_review(elems.nth(i), len(results) + 1)
                    except Exception:
                        continue

                    rid = review["id"]

                    # 최신순은 기존 리뷰를 만나면 바로 중단.
                    if sort_type == "LATEST" and rid in existing_ids:
                        stop = True
                        break

                    if rid in seen:
                        continue

                    if not review["content"] and review["author"] == "익명":
                        continue

                    seen.add(rid)
                    results.append(review)

                if stop or len(results) >= limit:
                    break

                stagnant = stagnant + 1 if len(results) == before else 0

                if stagnant >= 3:
                    break

                if not self.click_more(target):
                    target.wait_for_timeout(1000)

            return results[:limit]

        finally:
            page.close()


def push_error(place_name, sort_type, exc):
    try:
        post_api({
            "action": "push_error",
            "placeName": place_name,
            "sort": sort_type,
            "message": f"{type(exc).__name__}: {exc}",
        })
    except Exception:
        pass


def main():
    print("Fetching enabled places from Apps Script...")
    data = post_api({"action": "get_places"})
    places = data.get("places", [])

    if not places:
        print("No enabled places.")
        return 0

    print("Enabled places:", len(places))

    crawler = NaverReviewCrawler()
    crawler.start()

    failed = False

    try:
        for place in places:
            name = place["name"]
            url = place["naverUrl"]
            existing_ids = place.get("existingIds", [])

            print("\n==============================")
            print("PLACE:", name)
            print("==============================")

            try:
                latest = crawler.collect(
                    url,
                    "LATEST",
                    LATEST_LIMIT,
                    existing_ids,
                )

                print("LATEST collected:", len(latest))

                result = post_api({
                    "action": "push_reviews",
                    "placeName": name,
                    "sort": "LATEST",
                    "reviews": latest,
                })

                print("LATEST saved:", result)

            except Exception as exc:
                failed = True
                print("LATEST ERROR:", exc)
                traceback.print_exc()
                push_error(name, "LATEST", exc)

            time.sleep(2)

            try:
                recommend = crawler.collect(
                    url,
                    "RECOMMEND",
                    RECOMMEND_LIMIT,
                    [],
                )

                print("RECOMMEND collected:", len(recommend))

                result = post_api({
                    "action": "push_reviews",
                    "placeName": name,
                    "sort": "RECOMMEND",
                    "reviews": recommend,
                })

                print("RECOMMEND saved:", result)

            except Exception as exc:
                failed = True
                print("RECOMMEND ERROR:", exc)
                traceback.print_exc()
                push_error(name, "RECOMMEND", exc)

            time.sleep(2)

    finally:
        crawler.close()

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
