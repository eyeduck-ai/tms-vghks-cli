from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import CourseDetail, CourseItem, CourseSummary, ItemKind, ItemState, LoginStatus, SiteState
from .timeutils import parse_numeric_score, parse_passing_score, parse_required_seconds, parse_timer_to_seconds

BASE_URL = "https://tms.vghks.gov.tw"
LOGIN_PATH = "/index/login"
PENDING_PATH = "/course/notCompleteList"
COMPLETED_PATH = "/course/completeList"

LOGIN_MARKERS = ("登入", "驗證碼", "員工", "密碼")
PENDING_MARKERS = ("待修課程", "課程名稱", "完成度")
COMPLETED_MARKERS = ("已修課程", "已完成", "完成課程")
EMPTY_LIST_MARKERS = ("目前沒有待修課程", "沒有資料", "查無資料")
TRANSIENT_ERROR_MARKERS = ("儲存失敗", "請檢查伺服器狀態")
SEQUENCE_BLOCK_MARKERS = ("請依序完成", "依序完成")
PASS_MARKERS = ("通過", "已完成", "完成")


def normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def absolute_url(href: str | None, base_url: str = BASE_URL) -> str | None:
    if not href:
        return None
    return urljoin(base_url.rstrip("/") + "/", href)


def extract_course_id(url_or_text: str | None) -> str | None:
    if not url_or_text:
        return None
    parsed = urlparse(url_or_text)
    text = parsed.path or url_or_text
    patterns = [
        r"/course/(?:detail/|view/|info/)?(\d+)",
        r"(?:courseId|course_id|id)=([A-Za-z0-9_-]+)",
        r"\b(\d{3,})\b",
    ]
    query = parsed.query
    for pattern in patterns:
        match = re.search(pattern, text) or re.search(pattern, query)
        if match:
            return match.group(1)
    return None


def is_login_url(url: str | None) -> bool:
    return bool(url and LOGIN_PATH in url)


def classify_item_kind(title: str | None, pass_condition: str | None = None, raw_text: str | None = None) -> ItemKind:
    text = normalize_text(" ".join(part for part in (title, pass_condition, raw_text) if part))
    if any(token in text for token in ("問卷", "滿意度", "調查")):
        return ItemKind.SURVEY
    if any(token in text for token in ("測驗", "測試", "考試", "成績", "及格")):
        return ItemKind.QUIZ
    if any(token in text for token in ("影片", "影音", "video", "媒體", "觀看")):
        return ItemKind.VIDEO
    if any(token in text for token in ("閱讀", "教材", "讀取")):
        return ItemKind.READING
    return ItemKind.UNKNOWN


def result_satisfies_condition(
    pass_condition: str | None,
    result: str | None,
    passed_marker: str | None = None,
) -> bool:
    marker = normalize_text(passed_marker)
    if marker and marker not in {"-", "--", "否", "未通過", "未完成"}:
        if any(token in marker for token in PASS_MARKERS) or marker in {"Y", "YES", "V", "✓", "✔"}:
            return True

    condition = normalize_text(pass_condition)
    result_text = normalize_text(result)
    if not condition:
        return False
    if result_text in {"", "-", "--"}:
        return False

    passing_score = parse_passing_score(condition)
    if passing_score is not None:
        score = parse_numeric_score(result_text)
        return score is not None and score >= passing_score

    required_seconds = parse_required_seconds(condition)
    if required_seconds is not None:
        elapsed = parse_timer_to_seconds(result_text)
        return elapsed is not None and elapsed >= required_seconds

    if "須填寫" in condition:
        return any(token in result_text for token in PASS_MARKERS) or result_text not in {"", "-", "--"}
    return any(token in result_text for token in PASS_MARKERS)


def determine_item_state(item: CourseItem) -> ItemState:
    if result_satisfies_condition(item.pass_condition, item.result, item.passed_marker):
        return ItemState.PASSED
    if any(marker in item.raw_text for marker in SEQUENCE_BLOCK_MARKERS):
        return ItemState.BLOCKED
    if parse_required_seconds(item.pass_condition) is not None:
        elapsed = parse_timer_to_seconds(item.result)
        if elapsed is not None and elapsed > 0:
            return ItemState.IN_PROGRESS
    if item.result and normalize_text(item.result) not in {"", "-", "--"}:
        return ItemState.IN_PROGRESS
    return ItemState.PENDING


def classify_response(
    status_code: int | None,
    url: str | None,
    headers: Mapping[str, Any] | None = None,
    text: str | None = None,
    message: str = "",
) -> LoginStatus:
    headers = headers or {}
    location = str(headers.get("location") or headers.get("Location") or "")
    body = normalize_text(text)
    final_url = url or location or None

    if status_code is None:
        return LoginStatus(SiteState.UNKNOWN, final_url, status_code, message)
    if status_code == 0:
        return LoginStatus(SiteState.TIMEOUT, final_url, status_code, message or "request timed out")
    if 300 <= status_code < 400 and LOGIN_PATH in location:
        return LoginStatus(SiteState.LOGIN_REQUIRED, absolute_url(location), status_code, "redirected to login")
    if final_url and is_login_url(final_url):
        return LoginStatus(SiteState.LOGIN_REQUIRED, final_url, status_code, "login page")
    if any(marker in body for marker in TRANSIENT_ERROR_MARKERS):
        return LoginStatus(SiteState.TRANSIENT_ERROR, final_url, status_code, "transient TMS error")
    if status_code >= 500:
        return LoginStatus(SiteState.TRANSIENT_ERROR, final_url, status_code, f"server returned {status_code}")
    if body and all(marker in body for marker in ("登入", "密碼")) and "課程名稱" not in body:
        return LoginStatus(SiteState.LOGIN_REQUIRED, final_url, status_code, "login form detected")
    if any(marker in body for marker in PENDING_MARKERS + COMPLETED_MARKERS):
        return LoginStatus(SiteState.LOGGED_IN, final_url, status_code, "course page detected")
    if status_code == 200 and not body:
        return LoginStatus(SiteState.UNKNOWN, final_url, status_code, "empty response")
    return LoginStatus(SiteState.UNKNOWN, final_url, status_code, message or "unrecognized page")


def parse_course_list_html(
    html: str,
    base_url: str = BASE_URL,
    completed: bool | None = None,
) -> list[CourseSummary]:
    soup = BeautifulSoup(html or "", "html.parser")
    body_text = normalize_text(soup.get_text(" ", strip=True))
    if completed is False and any(marker in body_text for marker in EMPTY_LIST_MARKERS):
        return []
    courses: list[CourseSummary] = []
    seen: set[tuple[str | None, str]] = set()
    seen_urls: set[str] = set()

    for table in soup.find_all("table"):
        headers = _extract_table_headers(table)
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if not cells or all(cell.name == "th" for cell in cells):
                continue
            course = _course_from_cells(cells, headers, base_url, completed)
            if course:
                key = (course.detail_url, course.title)
                if key not in seen:
                    seen.add(key)
                    if course.detail_url:
                        seen_urls.add(course.detail_url)
                    courses.append(course)

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not _looks_like_course_detail_href(href):
            continue
        title = normalize_text(anchor.get_text(" ", strip=True))
        if not title or title in {"查看", "進入", "詳細"}:
            title = _nearby_course_title(anchor)
        if not title:
            continue
        url = absolute_url(href, base_url)
        if url in seen_urls:
            continue
        key = (url, title)
        if key not in seen:
            seen.add(key)
            if url:
                seen_urls.add(url)
            raw_text = normalize_text(anchor.parent.get_text(" ", strip=True) if anchor.parent else title)
            courses.append(
                CourseSummary(
                    title=title,
                    detail_url=url,
                    course_id=extract_course_id(url),
                    progress=_extract_progress(raw_text),
                    completed=completed,
                    raw_text=raw_text,
                )
            )
    return courses


def parse_course_detail_html(html: str, url: str, base_url: str = BASE_URL) -> CourseDetail:
    soup = BeautifulSoup(html or "", "html.parser")
    body_text = normalize_text(soup.get_text(" ", strip=True))
    title = _extract_page_title(soup)
    detail = CourseDetail(
        title=title,
        url=url,
        course_id=extract_course_id(url),
        completed="您已於" in body_text and "完成課程" in body_text,
        blockers=[marker for marker in SEQUENCE_BLOCK_MARKERS if marker in body_text],
        raw_text="",
    )

    items: list[CourseItem] = []
    seen: set[tuple[int | None, str]] = set()
    for table in soup.find_all("table"):
        headers = _extract_table_headers(table)
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if not cells or all(cell.name == "th" for cell in cells):
                continue
            item = _item_from_cells(cells, headers, base_url)
            if item and (item.order, item.title) not in seen:
                seen.add((item.order, item.title))
                items.append(item)

    if not items:
        for item in _items_from_activity_tree(soup, base_url):
            key = (item.order, item.title)
            if key not in seen:
                seen.add(key)
                items.append(item)

    detail.items = sorted(items, key=lambda item: (item.order is None, item.order or 0, item.title))
    detail.raw_text = _detail_activity_text(soup, detail.items, body_text)
    if detail.items and all(item.state == ItemState.PASSED for item in detail.items):
        detail.completed = True
    if not detail.items and any(marker in body_text for marker in ("課程內容", "通過條件", "學習成果")):
        detail.data_issues.append("course detail contained activity markers but no supported item structure was parsed")
    return detail


def _detail_activity_text(soup: BeautifulSoup, items: list[CourseItem], fallback_text: str) -> str:
    tree = soup.select_one("#activityTree")
    if tree:
        return normalize_text(tree.get_text(" ", strip=True))
    for table in soup.find_all("table"):
        table_text = normalize_text(table.get_text(" ", strip=True))
        if any(marker in table_text for marker in ("通過條件", "學習成果", "課程內容")):
            return table_text
    if items:
        return normalize_text(" ".join(item.raw_text for item in items))
    return fallback_text[:2000]


def _extract_table_headers(table: Tag) -> list[str]:
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        texts = [normalize_text(cell.get_text(" ", strip=True)) for cell in cells]
        if any(any(key in text for key in ("課程名稱", "通過條件", "學習成果", "完成度", "通過")) for text in texts):
            return texts
        if all(cell.name == "th" for cell in cells):
            return texts
    return []


def _course_from_cells(
    cells: list[Tag],
    headers: list[str],
    base_url: str,
    completed: bool | None,
) -> CourseSummary | None:
    row_text = normalize_text(" ".join(cell.get_text(" ", strip=True) for cell in cells))
    if not row_text:
        return None
    href = _first_course_href(cells)
    url = absolute_url(href, base_url) if href else None
    title = _field_by_header(cells, headers, ("課程名稱", "課程", "名稱"))
    if not title:
        link = next((cell.find("a", href=True) for cell in cells if cell.find("a", href=True)), None)
        title = normalize_text(link.get_text(" ", strip=True)) if link else ""
    if not title:
        title = _first_meaningful_cell(cells)
    if not title or title in {"查看", "詳細", "進入"}:
        return None
    if not url and not any(marker in row_text for marker in ("課程", "完成度", "待修", "已修")):
        return None
    progress = _field_by_header(cells, headers, ("完成度", "進度", "完成")) or _extract_progress(row_text)
    return CourseSummary(
        title=title,
        detail_url=url,
        course_id=extract_course_id(url or row_text),
        progress=progress,
        completed=completed,
        raw_text=row_text,
    )


def _item_from_cells(cells: list[Tag], headers: list[str], base_url: str) -> CourseItem | None:
    row_text = normalize_text(" ".join(cell.get_text(" ", strip=True) for cell in cells))
    if not row_text:
        return None
    if "通過條件" in row_text and "學習成果" in row_text and len(cells) <= 3:
        return None

    order = _extract_order(_field_by_header(cells, headers, ("項次", "順序", "序號")) or row_text)
    title = _field_by_header(cells, headers, ("項目", "名稱", "教材", "活動", "課程單元", "課程內容"))
    link = next((cell.find("a", href=True) for cell in cells if cell.find("a", href=True)), None)
    if not title and link:
        title = normalize_text(link.get_text(" ", strip=True))
    if not title:
        title = _infer_item_title_from_cells(cells)

    pass_condition = _field_by_header(cells, headers, ("通過條件", "條件"))
    if not pass_condition:
        pass_condition = _extract_condition_from_text(row_text)
    result = _field_by_header(cells, headers, ("學習成果", "成果", "成績", "結果"))
    passed_marker = _field_by_header(cells, headers, ("通過", "狀態", "完成"))
    href = link.get("href") if link else None

    if not title and not pass_condition:
        return None
    item = CourseItem(
        title=title or f"item-{order or len(row_text)}",
        order=order,
        kind=classify_item_kind(title, pass_condition, row_text),
        detail_url=absolute_url(href, base_url),
        pass_condition=pass_condition,
        result=result,
        passed_marker=passed_marker,
        raw_text=row_text,
    )
    item.state = determine_item_state(item)
    return item


def _items_from_activity_tree(soup: BeautifulSoup, base_url: str) -> list[CourseItem]:
    items: list[CourseItem] = []
    for fallback_order, node in enumerate(soup.select("#activityTree li.xtree-node"), start=1):
        item = _item_from_activity_node(node, fallback_order, base_url)
        if item:
            items.append(item)
    return items


def _item_from_activity_node(node: Tag, fallback_order: int, base_url: str) -> CourseItem | None:
    raw_text = normalize_text(node.get_text(" ", strip=True))
    if not raw_text:
        return None
    order = _extract_order(_text_of_first(node, ".sn")) or fallback_order
    title = _text_of_first(node, ".fs-singleLineText") or _text_of_first(node, ".node-title")
    title = _clean_activity_title(title)
    pass_condition = _text_of_first(node, ".col-char7") or _field_from_description(node, "通過條件")
    result = _text_of_first(node, ".col-char4") or _field_from_description(node, "學習成果")
    deadline = _text_of_first(node, ".col-date") or _field_from_description(node, "期限")
    passed_marker = "通過" if node.select_one(".item-pass, .fa-check-circle") or " 通過" in raw_text else None
    result_link = node.select_one(".col-char4 a[data-url], dd a[data-url]")
    result_modal_url = absolute_url(result_link.get("data-url"), base_url) if result_link else None
    result_modal_title = result_link.get("data-modal-title") if result_link else None
    result_modal_target = result_link.get("data-target") if result_link else None
    detail_anchor = node.select_one(".fs-singleLineText a[href], .node-title a[href]")
    detail_href = detail_anchor.get("href") if detail_anchor else None
    detail_url = absolute_url(detail_href, base_url) if detail_href and not detail_href.startswith("#") else None
    if not title and not pass_condition:
        return None
    item = CourseItem(
        title=title or f"item-{order}",
        order=order,
        kind=classify_item_kind(title, pass_condition, raw_text),
        state=ItemState.UNKNOWN,
        detail_url=detail_url,
        pass_condition=pass_condition,
        result=result,
        passed_marker=passed_marker,
        raw_text=raw_text,
        metadata={
            "source": "activity_tree",
            "activity_tree_id": node.get("id"),
            "activity_id": node.get("data-id") or node.get("id"),
            "activity_type": node.get("data-type"),
            "deadline": deadline,
            "result_modal_url": result_modal_url,
            "result_modal_title": result_modal_title,
            "result_modal_target": result_modal_target,
        },
    )
    item.state = determine_item_state(item)
    return item


def _text_of_first(node: Tag, selector: str) -> str | None:
    found = node.select_one(selector)
    if not found:
        return None
    return normalize_text(found.get_text(" ", strip=True))


def _field_from_description(node: Tag, label: str) -> str | None:
    for dt in node.find_all("dt"):
        if normalize_text(dt.get_text(" ", strip=True)) != label:
            continue
        dd = dt.find_next_sibling("dd")
        if dd:
            return normalize_text(dd.get_text(" ", strip=True))
    return None


def _clean_activity_title(title: str | None) -> str:
    text = normalize_text(title)
    if not text:
        return ""
    text = re.sub(r"^\d+[.)、]?\s*", "", text)
    text = re.split(r"\s+期限\s+", text, maxsplit=1)[0]
    text = re.split(r"\s+通過條件\s+", text, maxsplit=1)[0]
    text = re.split(r"\s+學習成果\s+", text, maxsplit=1)[0]
    return normalize_text(text)


def _field_by_header(cells: list[Tag], headers: list[str], names: tuple[str, ...]) -> str | None:
    for index, header in enumerate(headers[: len(cells)]):
        if any(name in header for name in names):
            return normalize_text(cells[index].get_text(" ", strip=True))
    return None


def _first_meaningful_cell(cells: list[Tag]) -> str:
    ignored = {"查看", "詳細", "進入", "開始", "繼續"}
    for cell in cells:
        text = normalize_text(cell.get_text(" ", strip=True))
        if text and text not in ignored and len(text) > 1:
            return text
    return ""


def _first_course_href(cells: list[Tag]) -> str | None:
    for cell in cells:
        for anchor in cell.find_all("a", href=True):
            href = anchor.get("href")
            if _looks_like_course_detail_href(href):
                return href
    return None


def _looks_like_course_detail_href(href: str | None) -> bool:
    if not href:
        return False
    if any(path in href for path in (PENDING_PATH, COMPLETED_PATH, LOGIN_PATH)):
        return False
    parsed = urlparse(href)
    path = parsed.path or href
    query = parsed.query or href
    if re.search(r"/course/(?:detail/|view/|info/)?\d+/?$", path):
        return True
    return bool(re.search(r"(?:courseId|course_id)=\d+", query))


def _nearby_course_title(anchor: Tag) -> str:
    parent = anchor.parent
    for _ in range(3):
        if not parent:
            break
        text = normalize_text(parent.get_text(" ", strip=True))
        text = re.sub(r"(查看|詳細|進入|開始|繼續)\s*$", "", text).strip()
        if len(text) > 2:
            return text
        parent = parent.parent
    return ""


def _extract_progress(text: str) -> str | None:
    match = re.search(r"(\d{1,3}\s*%)", text)
    if match:
        return match.group(1).replace(" ", "")
    match = re.search(r"完成度[:：]?\s*([^ ]+)", text)
    if match:
        return match.group(1)
    return None


def _extract_page_title(soup: BeautifulSoup) -> str:
    for selector in ("h1", "h2", ".course-title", ".title"):
        node = soup.select_one(selector)
        if node:
            text = normalize_text(node.get_text(" ", strip=True))
            if text:
                return text
    if soup.title:
        return normalize_text(soup.title.get_text(" ", strip=True))
    return "TMS course"


def _extract_order(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"(?:^|\D)(\d{1,3})(?:\D|$)", text)
    return int(match.group(1)) if match else None


def _infer_item_title_from_cells(cells: list[Tag]) -> str:
    for cell in cells:
        text = normalize_text(cell.get_text(" ", strip=True))
        if not text:
            continue
        if any(token in text for token in ("閱讀達", "及格", "須填寫", "通過", "學習成果")):
            continue
        if len(text) > 1:
            return re.sub(r"^\d+[.)、]?\s*", "", text).strip()
    return ""


def _extract_condition_from_text(text: str) -> str | None:
    patterns = [
        r"閱讀達\s*\d+\s*(?:分鐘|分|秒|小時|時)",
        r"觀看達\s*\d+\s*(?:分鐘|分|秒|小時|時)",
        r"\d+\s*分\s*及格",
        r"須填寫",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None
