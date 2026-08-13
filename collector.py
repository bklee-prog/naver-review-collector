import os
import re
import sys
import time
import hashlib
import traceback
from urllib.parse import urlparse, urljoin

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
    "[data-review-id]",
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
    """
    Apps Script ContentService는 script.google.com -> script.googleusercontent.com
    one-time URL로 redirect한다.
    requests의 자동 redirect에만 맡기지 않고, 원본 /exec URL에서 매번 새 redirect를 받아
    GET으로 결과를 회수한다. 404/5xx는 원본 URL부터 재시도한다.
    """
    payload = dict(payload)
    payload["token"] = INBOUND_TOKEN

    last_error = None

    for attempt in range(1, 4):
        try:
            first = requests.post(
                APPS_SCRIPT_URL,
                json=payload,
                timeout=180,
                allow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0 GitHubActions-NaverReviewCollector/5.2"},
            )

            # Apps Script ContentService의 일반적인 302/303 redirect
            if first.status_code in (301, 302, 303):
                location = first.headers.get("Location")
                if not location:
                    raise RuntimeError(
                        f"Apps Script redirect without Location: HTTP {first.status_code}"
                    )

                second = requests.get(
                    location,
                    timeout=180,
                    allow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 GitHubActions-NaverReviewCollector/5.2"},
                )

                if second.status_code == 404:
                    raise RuntimeError("Apps Script one-time response URL returned 404")

                second.raise_for_status()
                response = second

            elif first.status_code in (307, 308):
                location = first.headers.get("Location")
                if not location:
                    raise RuntimeError(
                        f"Apps Script redirect without Location: HTTP {first.status_code}"
                    )

                second = requests.post(
                    location,
                    json=payload,
                    timeout=180,
                    allow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 GitHubActions-NaverReviewCollector/5.2"},
                )
                second.raise_for_status()
                response = second

            else:
                first.raise_for_status()
                response = first

            try:
                data = response.json()
            except Exception:
                raise RuntimeError(
                    f"Apps Script returned non-JSON: HTTP {response.status_code} "
                    f"{response.text[:800]}"
                )

            if not data.get("ok"):
                raise RuntimeError(data.get("error") or "Apps Script API error")

            return data

        except Exception as exc:
            last_error = exc
            print(f"Apps Script API retry {attempt}/3:", exc)

            if attempt < 3:
                time.sleep(2 * attempt)

    raise last_error


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

    def find_frame(self, page, names):
        for _ in range(30):
            for frame in page.frames:
                if (frame.name or "") in names:
                    return frame
            page.wait_for_timeout(300)
        return None

    def find_entry_frame(self, page):
        for _ in range(30):
            for frame in page.frames:
                name = frame.name or ""
                url = frame.url or ""

                if name == "entryIframe":
                    return frame

                if "place.naver.com" in url and "/place/list" not in url:
                    return frame

            page.wait_for_timeout(300)

        return None

    def click_place_from_search_list(self, page, place_name):
        """
        map.naver.com/p/search/... URL이 pcmap.place.naver.com/place/list?... 로 열릴 때
        검색 결과 중 PLACE_NAME과 일치하는 업체를 클릭해서 상세 페이지로 진입.
        """
        print("Search-list page detected. Resolving place:", place_name)

        search_frame = self.find_frame(page, {"searchIframe"})
        target = search_frame if search_frame else page

        # pcmap의 place/list가 다른 frame으로 존재할 수 있음
        for frame in page.frames:
            if "/place/list" in (frame.url or ""):
                target = frame
                break

        # 결과가 로드될 시간을 조금 줌
        target.wait_for_timeout(1800)

        # 1) 업체명 정확 일치 텍스트
        candidates = [
            target.get_by_text(place_name, exact=True),
            target.get_by_role("link", name=re.compile(re.escape(place_name))),
        ]

        for loc in candidates:
            try:
                for i in range(min(loc.count(), 20)):
                    item = loc.nth(i)
                    if not item.is_visible():
                        continue

                    # 가능하면 실제 href를 얻어 상세 URL로 직접 이동
                    try:
                        href = item.evaluate("""
                            el => {
                              const a = el.closest('a');
                              if (a && a.href) return a.href;
                              const p = el.parentElement && el.parentElement.closest
                                ? el.parentElement.closest('a') : null;
                              return p && p.href ? p.href : '';
                            }
                        """)
                    except Exception:
                        href = ""

                    if href and "/place/" in href and "/place/list" not in href:
                        print("Resolved detail href:", href)
                        if target == page:
                            page.goto(href, wait_until="domcontentloaded", timeout=45000)
                        else:
                            target.goto(href, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(2200)
                        return True

                    # href 확보 실패 시 클릭
                    try:
                        item.click(timeout=4000)
                        page.wait_for_timeout(2500)
                        return True
                    except Exception:
                        pass
            except Exception:
                pass

        # 2) anchor 자체에서 업체명 포함 검색
        try:
            links = target.locator("a")
            for i in range(min(links.count(), 250)):
                link = links.nth(i)
                text = safe_text(link)

                if place_name and place_name in text and link.is_visible():
                    href = link.get_attribute("href") or ""

                    if href:
                        absolute = urljoin(target.url, href)
                        print("Resolved by anchor:", absolute)

                        if target == page:
                            page.goto(absolute, wait_until="domcontentloaded", timeout=45000)
                        else:
                            target.goto(absolute, wait_until="domcontentloaded", timeout=45000)

                        page.wait_for_timeout(2200)
                        return True

                    link.click(timeout=4000)
                    page.wait_for_timeout(2500)
                    return True
        except Exception:
            pass

        return False

    def open_review_area(self, page, naver_url, place_name):
        if not allowed_naver_url(naver_url):
            raise RuntimeError("PLACES의 NAVER_URL은 naver.com/naver.me 주소여야 합니다.")

        print("OPEN:", naver_url)

        page.goto(
            naver_url,
            wait_until="domcontentloaded",
            timeout=45000,
        )
        page.wait_for_timeout(3000)

        # 검색 URL이면 먼저 검색결과 -> 상세 페이지 진입
        list_frame = None
        for frame in page.frames:
            if "/place/list" in (frame.url or ""):
                list_frame = frame
                break

        if "/place/list" in (page.url or "") or list_frame:
            if not self.click_place_from_search_list(page, place_name):
                current = list_frame.url if list_frame else page.url
                raise RuntimeError(
                    "검색결과에서 업체 상세 페이지를 찾지 못했습니다. "
                    f"PLACE_NAME='{place_name}', target={current}"
                )

        # 상세 frame 재탐색
        frame = self.find_entry_frame(page)
        target = frame if frame else page

        # 여전히 리스트면 상세 진입 실패
        if "/place/list" in (target.url or ""):
            if not self.click_place_from_search_list(page, place_name):
                raise RuntimeError(
                    "장소 상세 페이지 진입에 실패했습니다. "
                    f"PLACE_NAME='{place_name}', target={target.url}"
                )

            frame = self.find_entry_frame(page)
            target = frame if frame else page

        print("DETAIL TARGET:", target.url)

        # 리뷰 탭 진입
        if "/review" not in (target.url or ""):
            clicked = False

            candidates = [
                target.get_by_text("리뷰", exact=True),
                target.get_by_role("link", name=re.compile(r"^리뷰")),
                target.get_by_role("button", name=re.compile(r"^리뷰")),
            ]

            for loc in candidates:
                try:
                    for i in range(min(loc.count(), 10)):
                        item = loc.nth(i)

                        if item.is_visible():
                            item.click(timeout=4000)
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

        # 리뷰 목록 대기
        found = False

        for selector in REVIEW_LIST_SELECTORS:
            try:
                target.locator(selector).first.wait_for(
                    state="attached",
                    timeout=9000,
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
                "장소 상세 페이지 진입은 했지만 리뷰 DOM selector가 현재 화면과 맞지 않습니다. "
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

    def collect_with_retry(self, place_name, naver_url, sort_type, limit, existing_ids, attempts=3):
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                print(f"{sort_type} browser attempt {attempt}/{attempts}")
                return self.collect(
                    place_name,
                    naver_url,
                    sort_type,
                    limit,
                    existing_ids,
                )
            except Exception as exc:
                last_error = exc
                print(f"{sort_type} attempt {attempt} failed:", exc)

                if attempt < attempts:
                    time.sleep(3 * attempt)

        raise last_error

    def collect(self, place_name, naver_url, sort_type, limit, existing_ids):
        sort_type = sort_type.upper()
        limit = max(1, min(int(limit), 500))
        existing_ids = set(str(x) for x in (existing_ids or []))

        page = self.context.new_page()

        try:
            target = self.open_review_area(page, naver_url, place_name)
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
    success_count = 0
    failure_count = 0

    try:
        for place in places:
            name = place["name"]
            url = place["naverUrl"]
            existing_ids = place.get("existingIds", [])

            print("\n==============================")
            print("PLACE:", name)
            print("==============================")

            try:
                latest = crawler.collect_with_retry(
                    name,
                    url,
                    "LATEST",
                    LATEST_LIMIT,
                    existing_ids,
                    attempts=3,
                )

                print("LATEST collected:", len(latest))

                result = post_api({
                    "action": "push_reviews",
                    "placeName": name,
                    "sort": "LATEST",
                    "reviews": latest,
                })

                print("LATEST saved:", result)
                success_count += 1

            except Exception as exc:
                failed = True
                failure_count += 1
                print("LATEST ERROR:", exc)
                traceback.print_exc()
                push_error(name, "LATEST", exc)

            time.sleep(2)

            try:
                recommend = crawler.collect_with_retry(
                    name,
                    url,
                    "RECOMMEND",
                    RECOMMEND_LIMIT,
                    [],
                    attempts=3,
                )

                print("RECOMMEND collected:", len(recommend))

                result = post_api({
                    "action": "push_reviews",
                    "placeName": name,
                    "sort": "RECOMMEND",
                    "reviews": recommend,
                })

                print("RECOMMEND saved:", result)
                success_count += 1

            except Exception as exc:
                failed = True
                failure_count += 1
                print("RECOMMEND ERROR:", exc)
                traceback.print_exc()
                push_error(name, "RECOMMEND", exc)

            time.sleep(2)

    finally:
        crawler.close()

    print("\n==============================")
    print("RUN SUMMARY")
    print("==============================")
    print("Successful collection pushes:", success_count)
    print("Failed collection attempts:", failure_count)

    # 일부 업체/정렬이 실패해도 성공 데이터가 하나라도 있으면
    # GitHub Actions 자체는 성공으로 처리하고 FETCH_LOG에서 개별 실패를 확인.
    # 전부 실패한 경우에만 workflow를 실패시킨다.
    return 0 if success_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
