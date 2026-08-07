import argparse
from email import feedparser
import json
import os
import re
import urllib.parse
import pendulum
from retrying import retry
import requests
from douban2notion.notion_helper import NotionHelper
from douban2notion import utils
DOUBAN_API_HOST = os.getenv("DOUBAN_API_HOST", "frodo.douban.com")
DOUBAN_API_KEY = os.getenv("DOUBAN_API_KEY", "0ac44ae016490db2204ce0a042db2916")

from douban2notion.config import movie_properties_type_dict,book_properties_type_dict, TAG_ICON_URL, USER_ICON_URL
from douban2notion.utils import get_icon
from dotenv import load_dotenv
load_dotenv()
rating = {
    1: "⭐️",
    2: "⭐️⭐️",
    3: "⭐️⭐️⭐️",
    4: "⭐️⭐️⭐️⭐️",
    5: "⭐️⭐️⭐️⭐️⭐️",
}
movie_status = {
    "mark": "想看",
    "doing": "在看",
    "done": "看过",
}
book_status = {
    "mark": "想读",
    "doing": "在读",
    "done": "读过",
}
AUTH_TOKEN = os.getenv("AUTH_TOKEN")

headers = {
    "host": DOUBAN_API_HOST,
    "authorization": f"Bearer {AUTH_TOKEN}" if AUTH_TOKEN else "",
    "user-agent": "User-Agent: Mozilla/5.0 (iPhone; CPU iPhone OS 15_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.16(0x18001023) NetType/WIFI Language/zh_CN",
    "referer": "https://servicewechat.com/wx2f9b06c1de1ccfca/84/page-frame.html",
}
@retry(stop_max_attempt_number=3, wait_fixed=5000)
def fetch_subjects(user, type_, status):
    offset = 0
    page = 0
    url = f"https://{DOUBAN_API_HOST}/api/v2/user/{user}/interests"
    total = 0
    results = []
    while True:
        params = {
            "type": type_,
            "count": 50,
            "status": status,
            "start": offset,
            "apiKey": DOUBAN_API_KEY,
        }
        response = requests.get(url, headers=headers, params=params)
        
        if response.ok:
            response = response.json()
            interests = response.get("interests")
            if len(interests)==0:
                break
            results.extend(interests)
            print(f"total = {total}")
            print(f"size = {len(results)}")
            page += 1
            offset = page * 50
    return results


def imdb_search(title, year):
    """按片名搜 IMDb suggestion API，优先年份匹配，返回 tt 编号或 None"""
    if not title:
        return None
    imdb_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        query = urllib.parse.quote(title)
        r = requests.get(
            f"https://v3.sg.media-imdb.com/suggestion/t/{query}.json",
            headers=imdb_headers,
            timeout=10,
        )
        if not r.ok:
            return None
        items = r.json().get("d", [])
    except Exception:
        return None
    # 优先年份匹配的影视条目
    for it in items:
        if not str(it.get("id", "")).startswith("tt"):
            continue
        if it.get("q") in ("person", "name"):
            continue
        if year and it.get("y") is not None and int(it.get("y")) == int(year):
            return it["id"]
    # 年份未匹配：取第一个影视条目兜底
    for it in items:
        if str(it.get("id", "")).startswith("tt") and it.get("q") not in ("person", "name"):
            return it["id"]
    return None


def extract_cn_title(raw):
    """从豆瓣 title（'中文译名 原语言名 (年份)'）提取纯中文名。
    保留季数（第二季/Part.2/LOST GIRLS 等），砍掉日文/韩文/英文原语言部分。"""
    if not raw:
        return None
    t = re.sub(r"\s*\(\d{4}\)\s*$", "", raw).strip()
    # 1. 日文假名 / 韩文 = 原语言起点；向前扩展紧邻汉字（僕の、進撃の 都属于原语言）
    m = re.search(r"[\u3040-\u30ff\uac00-\ud7af]", t)
    if m:
        start = m.start()
        while start > 0 and re.match(r"[\u4e00-\u9fff]", t[start - 1]):
            start -= 1
        # 假名前紧邻非空格非汉字（如 "Re:ゼロ" 的冒号）→ 是原语言前缀，再往前砍到空格
        if start > 0 and t[start - 1] != " ":
            cut = t.rfind(" ", 0, start)
            start = cut + 1 if cut >= 0 else 0
        return t[:start].strip() or None
    # 2. 无假名/韩文：拉丁词分界（排除 Part.x 季号后缀；第一个词纯拉丁如 "Lady" 时不砍）
    parts = t.split(" ")
    if len(parts) > 1 and re.search(r"[\u4e00-\u9fff]", parts[0]):
        for i in range(1, len(parts)):
            if re.match(r"[A-Za-z]", parts[i]) and not parts[i].startswith("Part"):
                return " ".join(parts[:i]).strip()
    return t


def fix_unknown_title(subject):
    """豆瓣 interests 列表接口对部分条目返回占位标题"未知电影/未知电视剧"（下架/特殊条目）。
    用 j/subject_abstract（含中文译名）提取真实中文名，j/subject（原语言名）兜底。"""
    sid = subject.get("id")
    if not sid:
        return
    try:
        r = requests.get(
            f"https://movie.douban.com/j/subject_abstract?subject_id={sid}",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=10,
        )
        if r.ok:
            sub = r.json().get("subject") or {}
            title = extract_cn_title(sub.get("title"))
            if title:
                subject["title"] = title
                return
    except Exception:
        pass
    try:
        r = requests.get(
            f"https://movie.douban.com/j/subject/{sid}/",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            if data.get("title"):
                subject["title"] = data["title"]
    except Exception:
        pass


@retry(stop_max_attempt_number=3, wait_fixed=5000)
def fetch_imdb_id(subject):
    """获取 IMDb 编号：豆瓣片名（中文可搜）→ 未命中用 original_title 英文名再搜"""
    sid = subject.get("id")
    if not sid:
        return None
    # 详情接口拿年份与原始标题（列表接口的 subject 不带 year/original_title）
    stype = subject.get("type")
    api_type = "tv" if stype == "tv" else "movie"
    title = subject.get("title")
    original_title = None
    year = None
    try:
        url = f"https://{DOUBAN_API_HOST}/api/v2/{api_type}/{sid}"
        params = {"apiKey": DOUBAN_API_KEY}
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.ok:
            data = response.json()
            title = data.get("title") or title
            original_title = data.get("original_title")
            year = data.get("year")
    except Exception:
        pass
    if not title:
        return None
    imdb = imdb_search(title, year)
    if imdb:
        return imdb
    # 兜底：英文原名（处理 IMDb 中文索引缺失的条目）
    if original_title and original_title != title:
        return imdb_search(original_title, year)
    return None



def insert_movie(douban_name,notion_helper):
    notion_movies = notion_helper.query_all(database_id=notion_helper.movie_database_id)
    notion_movie_dict = {}
    for i in notion_movies:
        movie = {}
        for key, value in i.get("properties").items():
            movie[key] = utils.get_property_value(value)
        notion_movie_dict[movie.get("豆瓣链接")] = {
            "电影名": movie.get("电影名"),
            "短评": movie.get("短评"),
            "状态": movie.get("状态"),
            "日期": movie.get("日期"),
            "评分": movie.get("评分"),
            "演员": movie.get("演员"),
            "IMDB": movie.get("IMDB"),
            "封面": movie.get("封面"),
            "page_id": i.get("id")
        }
    results = []
    for i in movie_status.keys():
        results.extend(fetch_subjects(douban_name, "movie", i))
    # 按 subject id 去重，避免豆瓣接口返回重复条目
    seen = set()
    dedup = []
    for r in results:
        if not r:
            continue
        key = (r.get("subject") or {}).get("id")
        if key in seen:
            continue
        seen.add(key)
        dedup.append(r)
    results = dedup
    updated = 0
    seq = 0
    for result in results:
        movie = {}
        if not result:
            print(result)
            continue
        subject = result.get("subject")
        # 豆瓣列表接口对下架/特殊条目返回占位标题，用 j/subject 补真实标题
        if subject and subject.get("title") in ("未知电影", "未知电视剧"):
            fix_unknown_title(subject)
        movie["电影名"] = subject.get("title")
        create_time = result.get("create_time")
        create_time = pendulum.parse(create_time,tz=utils.tz)
        #时间上传到Notion会丢掉秒的信息，这里直接将秒设置为0
        create_time = create_time.replace(second=0)
        movie["日期"] = create_time.int_timestamp
        movie["豆瓣链接"] = subject.get("url")
        movie["状态"] = movie_status.get(result.get("status"))
        if result.get("rating"):
            movie["评分"] = rating.get(result.get("rating").get("value"))
        if result.get("comment"):
            movie["短评"] = result.get("comment")
        if notion_movie_dict.get(movie.get("豆瓣链接")):
            notion_movive = notion_movie_dict.get(movie.get("豆瓣链接"))
            cover = (subject.get("pic") or {}).get("normal") or ""
            if not cover:
                # 豆瓣无封面图：不动 Notion 封面
                movie.pop("封面", None)
                cover_status = "无图"
            else:
                if not cover.endswith('.webp'):
                    cover = cover.rsplit('.', 1)[0] + '.webp'
                # 封面增量：Notion 里已是托管（file_upload id 或 Notion CDN url）则跳过上传；
                # 只有豆瓣外链（doubanio.com）或空封面才需要上传迁移
                notion_cover = notion_movive.get("封面") or ""
                if not notion_cover or "doubanio.com" in notion_cover:
                    cover_id = notion_helper.upload_cover(cover)
                    if cover_id and not str(cover_id).startswith("http"):
                        movie["封面"] = cover_id
                        cover_status = "已上传"
                    elif cover_id:
                        movie["封面"] = cover_id
                        cover_status = "上传失败"
                    else:
                        movie.pop("封面", None)
                        cover_status = "上传失败"
                else:
                    # 已是 Notion 托管：不写回（CDN URL 会过期），保持原样
                    movie.pop("封面", None)
                    cover_status = "已托管"
            seq += 1
            print(f"[{seq}] {movie.get('电影名')} | 封面:{cover_status}")
            if (
                notion_movive.get("日期") != movie.get("日期")
                or notion_movive.get("短评") != movie.get("短评")
                or notion_movive.get("状态") != movie.get("状态")
                or notion_movive.get("评分") != movie.get("评分")
                or (not notion_movive.get("演员") and subject.get("actors"))
                or (
                    notion_movive.get("电影名") in ("未知电影", "未知电视剧")
                    and movie.get("电影名") not in ("未知电影", "未知电视剧", None)
                )
                or (movie.get("封面") and notion_movive.get("封面") != movie.get("封面"))
            ):
                if not notion_movive.get("演员") and subject.get("actors"):
                    l = []
                    actors = subject.get("actors")[0:5]
                    for actor in actors:
                        if actor.get("name"):
                            if "/" in actor.get("name"):
                                l.extend(actor.get("name").split("/"))
                            else:
                                l.append(actor.get("name"))  
                    movie["演员"] = [
                        notion_helper.get_relation_id(
                            x.get("name"), notion_helper.actor_database_id, USER_ICON_URL
                        )
                        for x in actors
                    ]
                properties = utils.get_properties(movie, movie_properties_type_dict)
                notion_helper.get_date_relation(properties,create_time)
                notion_helper.update_page(
                    page_id=notion_movive.get("page_id"),
                    properties=properties,
                    icon=get_icon(movie.get("封面")) if movie.get("封面") else None
            )
                updated += 1

        else:
            cover = (subject.get("pic") or {}).get("normal") or ""
            if cover:
                if not cover.endswith('.webp'):
                    cover = cover.rsplit('.', 1)[0] + '.webp'
                cover_id = notion_helper.upload_cover(cover)
                if cover_id and not str(cover_id).startswith("http"):
                    movie["封面"] = cover_id
                    cover_status = "已上传"
                elif cover_id:
                    movie["封面"] = cover_id
                    cover_status = "上传失败"
                else:
                    cover_status = "上传失败"
            else:
                cover_status = "无图"
            seq += 1
            print(f"[{seq}] {movie.get('电影名')} | 封面:{cover_status} | 插入")
            movie["类型"] = subject.get("type")
            imdb = subject.get("imdb_id") or fetch_imdb_id(subject)
            if imdb:
                movie["IMDB"] = imdb
            if subject.get("genres"):
                movie["分类"] = [
                    notion_helper.get_relation_id(
                        x, notion_helper.category_database_id, TAG_ICON_URL
                    )
                    for x in subject.get("genres")
                ]
            if subject.get("actors"):
                l = []
                actors = subject.get("actors")[0:5]
                for actor in actors:
                    if actor.get("name"):
                        if "/" in actor.get("name"):
                            l.extend(actor.get("name").split("/"))
                        else:
                            l.append(actor.get("name"))  
                movie["演员"] = [
                    notion_helper.get_relation_id(
                        x.get("name"), notion_helper.actor_database_id, USER_ICON_URL
                    )
                    for x in actors
                ]
            if subject.get("directors"):
                movie["导演"] = [
                    notion_helper.get_relation_id(
                        x.get("name"), notion_helper.director_database_id, USER_ICON_URL
                    )
                    for x in subject.get("directors")[0:5]
                ]
            properties = utils.get_properties(movie, movie_properties_type_dict)
            notion_helper.get_date_relation(properties,create_time)
            parent = {
                "database_id": notion_helper.movie_database_id,
                "type": "database_id",
            }
            notion_helper.create_page(
                parent=parent, properties=properties, icon=get_icon(movie.get("封面")) if movie.get("封面") else None
            )
    print(f"同步完成，更新 {updated} 条")


def insert_book(douban_name,notion_helper):
    notion_books = notion_helper.query_all(database_id=notion_helper.book_database_id)
    notion_book_dict = {}
    for i in notion_books:
        book = {}
        for key, value in i.get("properties").items():
            book[key] = utils.get_property_value(value)
        notion_book_dict[book.get("豆瓣链接")] = {
            "短评": book.get("短评"),
            "状态": book.get("状态"),
            "日期": book.get("日期"),
            "评分": book.get("评分"),
            "封面": book.get("封面"),
            "page_id": i.get("id"),
        }
        print(i)
    print(f"notion {len(notion_book_dict)}")
    results = []
    for i in book_status.keys():
        results.extend(fetch_subjects(douban_name, "book", i))
    seq = 0
    for result in results:
        book = {}
        if not result:
            continue
        subject = result.get("subject")
        book["书名"] = subject.get("title")
        create_time = result.get("create_time")
        create_time = pendulum.parse(create_time,tz=utils.tz)
        #时间上传到Notion会丢掉秒的信息，这里直接将秒设置为0
        create_time = create_time.replace(second=0)
        book["日期"] = create_time.int_timestamp
        book["豆瓣链接"] = subject.get("url")
        book["状态"] = book_status.get(result.get("status"))
        cover = (subject.get("pic") or {}).get("large") or ""
        if cover:
            if not cover.endswith('.webp'):
                cover = cover.rsplit('.', 1)[0] + '.webp'
            # 封面增量：Notion 里已是托管（file_upload id 或 Notion CDN url）则跳过上传
            notion_book = notion_book_dict.get(book.get("豆瓣链接"))
            notion_cover = notion_book.get("封面") if notion_book else None
            if notion_cover and "doubanio.com" not in str(notion_cover):
                book.pop("封面", None)
                cover_status = "已托管"
            else:
                cover_id = notion_helper.upload_cover(cover)
                if cover_id and not str(cover_id).startswith("http"):
                    book["封面"] = cover_id
                    cover_status = "已上传"
                elif cover_id:
                    book["封面"] = cover_id
                    cover_status = "上传失败"
                else:
                    cover_status = "上传失败"
        else:
            cover_status = "无图"
        if result.get("rating"):
            book["评分"] = rating.get(result.get("rating").get("value"))
        if result.get("comment"):
            book["短评"] = result.get("comment")
        if notion_book_dict.get(book.get("豆瓣链接")):
            notion_movive = notion_book_dict.get(book.get("豆瓣链接"))
            seq += 1
            print(f"[{seq}] {book.get('书名')} | 封面:{cover_status}")
            if (
                notion_movive.get("封面") is None
                or (book.get("封面") and notion_movive.get("封面") != book.get("封面"))
                or notion_movive.get("日期") != book.get("日期")
                or notion_movive.get("短评") != book.get("短评")
                or notion_movive.get("状态") != book.get("状态")
                or notion_movive.get("评分") != book.get("评分")
            ):
                properties = utils.get_properties(book, book_properties_type_dict)
                notion_helper.get_date_relation(properties,create_time)
                notion_helper.update_page(
                    page_id=notion_movive.get("page_id"),
                    properties=properties,
                    icon=get_icon(book.get("封面")) if book.get("封面") else None
            )

        else:
            seq += 1
            print(f"[{seq}] {book.get('书名')} | 封面:{cover_status} | 插入")
            book["简介"] = subject.get("intro")
            press = []
            for i in subject.get("press"):
                press.extend(i.split(","))
            book["出版社"] = press
            book["类型"] = subject.get("type")
            if result.get("tags"):
                book["分类"] = [
                    notion_helper.get_relation_id(
                        x, notion_helper.category_database_id, TAG_ICON_URL
                    )
                    for x in result.get("tags")
                ]
            if subject.get("author"):
                book["作者"] = [
                    notion_helper.get_relation_id(
                        x, notion_helper.author_database_id, USER_ICON_URL
                    )
                    for x in subject.get("author")[0:100]
                ]
            properties = utils.get_properties(book, book_properties_type_dict)
            notion_helper.get_date_relation(properties,create_time)
            parent = {
                "database_id": notion_helper.book_database_id,
                "type": "database_id",
            }
            notion_helper.create_page(
                parent=parent, properties=properties, icon=get_icon(book.get("封面")) if book.get("封面") else None
            )

     
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("type")
    options = parser.parse_args()
    type = options.type
    notion_helper = NotionHelper(type)
    is_movie = True if type=="movie" else False
    douban_name = os.getenv("DOUBAN_NAME", None)
    if is_movie:
        insert_movie(douban_name,notion_helper)
    else:
        insert_book(douban_name,notion_helper)
    notion_helper.save_cover_map()
if __name__ == "__main__":
    main()
