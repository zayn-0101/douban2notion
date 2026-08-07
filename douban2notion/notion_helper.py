import logging
import os
import re
import json
import requests

from notion_client import Client
from retrying import retry

from douban2notion.utils import (
    format_date,
    get_date,
    get_first_and_last_day_of_month,
    get_first_and_last_day_of_week,
    get_first_and_last_day_of_year,
    get_icon,
    get_relation,
    get_title,
)

TAG_ICON_URL = "https://www.notion.so/icons/tag_gray.svg"
USER_ICON_URL = "https://www.notion.so/icons/user-circle-filled_gray.svg"
TARGET_ICON_URL = "https://www.notion.so/icons/target_red.svg"
BOOKMARK_ICON_URL = "https://www.notion.so/icons/bookmark_gray.svg"


class NotionHelper:
    database_name_dict = {
        "MOVIE_DATABASE_NAME": "电影",
        "BOOK_DATABASE_NAME": "书架",
        "DAY_DATABASE_NAME": "日",
        "WEEK_DATABASE_NAME": "周",
        "MONTH_DATABASE_NAME": "月",
        "YEAR_DATABASE_NAME": "年",
        "CATEGORY_DATABASE_NAME": "分类",
        "DIRECTOR_DATABASE_NAME": "导演",
        "ACTOR_DATABASE_NAME": "演员",
        "AUTHOR_DATABASE_NAME": "作者",
    }
    database_id_dict = {}
    image_dict = {}
    def __init__(self,type):
        is_movie = True if type=="movie" else False
        page_url = os.getenv("NOTION_MOVIE_URL") if is_movie else os.getenv("NOTION_BOOK_URL")
        notion_token = os.getenv("NOTION_TOKEN")
        if not notion_token:
            if is_movie:
                notion_token = os.getenv("MOVIE_NOTION_TOKEN")
            else:
                notion_token = os.getenv("BOOK_NOTION_TOKEN")
        self.client = Client(auth=notion_token, log_level=logging.ERROR)
        self.__cache = {}
        # 封面 file_upload id 映射（源 URL -> Notion file_upload id），跨运行去重，避免每天重传导致 Notion 文件对象膨胀
        self.cover_map = self._load_cover_map()
        # 默认值，避免页面中不存在热力图 embed 块时抛 AttributeError
        self.heatmap_block_id = None
        self.page_id = self.extract_page_id(page_url)
        self.search_database(self.page_id)
        for key in self.database_name_dict.keys():
            if os.getenv(key) != None and os.getenv(key) != "":
                self.database_name_dict[key] = os.getenv(key)
        self.book_database_id = self.database_id_dict.get(
            self.database_name_dict.get("BOOK_DATABASE_NAME")
        )
        self.movie_database_id = self.database_id_dict.get(
            self.database_name_dict.get("MOVIE_DATABASE_NAME")
        )
        self.day_database_id = self.database_id_dict.get(
            self.database_name_dict.get("DAY_DATABASE_NAME")
        )
        self.week_database_id = self.database_id_dict.get(
            self.database_name_dict.get("WEEK_DATABASE_NAME")
        )
        self.month_database_id = self.database_id_dict.get(
            self.database_name_dict.get("MONTH_DATABASE_NAME")
        )
        self.year_database_id = self.database_id_dict.get(
            self.database_name_dict.get("YEAR_DATABASE_NAME")
        )
        self.category_database_id = self.database_id_dict.get(
            self.database_name_dict.get("CATEGORY_DATABASE_NAME")
        )
        self.director_database_id = self.database_id_dict.get(
            self.database_name_dict.get("DIRECTOR_DATABASE_NAME")
        )
        self.author_database_id = self.database_id_dict.get(
            self.database_name_dict.get("AUTHOR_DATABASE_NAME")
        )      
        self.actor_database_id = self.database_id_dict.get(
            self.database_name_dict.get("ACTOR_DATABASE_NAME")
        )
        if self.day_database_id:
            self.write_database_id(self.day_database_id)
        if is_movie:
            self.update_movie_database()

    def write_database_id(self, database_id):
        env_file = os.getenv('GITHUB_ENV')
        # 将值写入环境文件
        with open(env_file, "a") as file:
            file.write(f"DATABASE_ID={database_id}\n")
    def extract_page_id(self, notion_url):
        # 正则表达式匹配 32 个字符的 Notion page_id
        match = re.search(
            r"([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
            notion_url,
        )
        if match:
            return match.group(0)
        else:
            raise Exception(f"获取NotionID失败，请检查输入的Url是否正确")

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def search_database(self, block_id):
        children = self.client.blocks.children.list(block_id=block_id)["results"]
        # 遍历子块
        for child in children:
            # 检查子块的类型

            if child["type"] == "child_database":
                self.database_id_dict[
                    child.get("child_database").get("title")
                ] = child.get("id")
            elif child["type"] == "embed" and child.get("embed").get("url"):
                embed_url = child.get("embed").get("url")
                # 兼容三代 URL：heatmap.malinkang.com(旧) / OUT_FOLDER(产物分支) /
                # .notionhub-artifacts(7-27 重构版)
                if any(
                    keyword in embed_url
                    for keyword in ("heatmap", "OUT_FOLDER", "notionhub-artifacts")
                ):
                    self.heatmap_block_id = child.get("id")
            # 如果子块有子块，递归调用函数
            if "has_children" in child and child["has_children"]:
                self.search_database(child["id"])

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_heatmap(self, block_id, url):
        # 更新 image block 的链接
        return self.client.blocks.update(block_id=block_id, embed={"url": url})

    def get_week_relation_id(self, date):
        year = date.isocalendar().year
        week = date.isocalendar().week
        week = f"{year}年第{week}周"
        start, end = get_first_and_last_day_of_week(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            week, self.week_database_id, TARGET_ICON_URL, properties
        )

    def get_month_relation_id(self, date):
        month = date.strftime("%Y年%-m月")
        start, end = get_first_and_last_day_of_month(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            month, self.month_database_id, TARGET_ICON_URL, properties
        )

    def get_year_relation_id(self, date):
        year = date.strftime("%Y")
        start, end = get_first_and_last_day_of_year(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            year, self.year_database_id, TARGET_ICON_URL, properties
        )

    def get_day_relation_id(self, date):
        new_date = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day = new_date.strftime("%Y年%m月%d日")
        properties = {
            "日期": get_date(format_date(date)),
        }
        properties["年"] = get_relation(
            [
                self.get_year_relation_id(new_date),
            ]
        )
        properties["月"] = get_relation(
            [
                self.get_month_relation_id(new_date),
            ]
        )
        properties["周"] = get_relation(
            [
                self.get_week_relation_id(new_date),
            ]
        )
        return self.get_relation_id(
            day, self.day_database_id, TARGET_ICON_URL, properties
        )
    
    def update_movie_database(self):
        """更新数据库"""
        response = self.client.databases.retrieve(database_id=self.movie_database_id)
        id = response.get("id")
        properties = response.get("properties")
        update_properties = {}
        if (
            properties.get("演员") is None
            or properties.get("演员").get("type") != "relation"
        ):
            update_properties["演员"] = {"relation": {"database_id": self.actor_database_id,"dual_property":{}}}
        if (
            properties.get("IMDB") is None
            or properties.get("IMDB").get("type") != "rich_text"
        ):
            update_properties["IMDB"] = {"rich_text": {}}
        if len(update_properties) > 0:
            self.client.databases.update(database_id=id, properties=update_properties)
    
    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def get_relation_id(self, name, id, icon, properties={}):
        key = f"{id}{name}"
        if key in self.__cache:
            return self.__cache.get(key)
        filter = {"property": "标题", "title": {"equals": name}}
        response = self.client.databases.query(database_id=id, filter=filter)
        if len(response.get("results")) == 0:
            parent = {"database_id": id, "type": "database_id"}
            properties["标题"] = get_title(name)
            page_id = self.client.pages.create(
                parent=parent, properties=properties, icon=get_icon(icon)
            ).get("id")
        else:
            page_id = response.get("results")[0].get("id")
        self.__cache[key] = page_id
        return page_id



    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_book_page(self, page_id, properties):
        return self.client.pages.update(page_id=page_id, properties=properties)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_page(self, page_id, properties, icon=None):
        body = {"properties": properties}
        if icon is not None:
            body["icon"] = icon
        return self.client.pages.update(page_id=page_id, **body)

    def _load_cover_map(self):
        try:
            with open("cover_map.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_cover_map(self):
        try:
            with open("cover_map.json", "w", encoding="utf-8") as f:
                json.dump(self.cover_map, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 写回 cover_map.json 失败: {e}")

    def upload_cover(self, url):
        """下载豆瓣封面（带 Referer 绕过 418 防盗链）并上传到 Notion 自托管，
        返回 file_upload id。同一源 URL 在本运行内记忆化；跨运行由 cover_map 去重。
        失败时回退为原始外链 URL，保证不崩溃。"""
        if not url:
            return url
        if url in self.cover_map:
            return self.cover_map[url]
        token = (
            os.getenv("NOTION_TOKEN")
            or os.getenv("MOVIE_NOTION_TOKEN")
            or os.getenv("BOOK_NOTION_TOKEN")
        )
        if not token:
            return url
        try:
            dl = requests.get(
                url,
                headers={
                    "user-agent": "Mozilla/5.0",
                    "referer": "https://movie.douban.com/",
                },
                timeout=30,
            )
            dl.raise_for_status()
            content = dl.content
            content_type = (
                "image/webp"
                if url.endswith(".webp")
                else (dl.headers.get("content-type") or "image/jpeg")
            )
            headers = {
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            }
            create = requests.post(
                "https://api.notion.com/v1/file_uploads",
                headers=headers,
                json={
                    "mode": "single_part",
                    "filename": "cover.webp",
                    "content_type": content_type,
                },
                timeout=30,
            )
            create.raise_for_status()
            upload_id = create.json()["id"]
            # Send 步骤要求 multipart/form-data（file 字段），不能用裸二进制；
            # requests 的 files 参数会自动生成 boundary，勿手动设 Content-Type
            send = requests.post(
                f"https://api.notion.com/v1/file_uploads/{upload_id}/send",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": "2022-06-28",
                },
                files={"file": ("cover.webp", content, content_type)},
                timeout=60,
            )
            send.raise_for_status()
            self.cover_map[url] = upload_id
            print(f"封面已上传 Notion: {upload_id}")
            return upload_id
        except Exception as e:
            print(f"⚠️ 封面上传失败，回退外链: {e}")
            return url

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def create_page(self, parent, properties, icon):
        return self.client.pages.create(parent=parent, properties=properties, icon=icon)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query(self, **kwargs):
        kwargs = {k: v for k, v in kwargs.items() if v}
        return self.client.databases.query(**kwargs)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def get_block_children(self, id):
        response = self.client.blocks.children.list(id)
        return response.get("results")

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def append_blocks(self, block_id, children):
        return self.client.blocks.children.append(block_id=block_id, children=children)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def append_blocks_after(self, block_id, children, after):
        return self.client.blocks.children.append(
            block_id=block_id, children=children, after=after
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def delete_block(self, block_id):
        return self.client.blocks.delete(block_id=block_id)


    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query_all_by_book(self, database_id, filter):
        results = []
        has_more = True
        start_cursor = None
        while has_more:
            response = self.client.databases.query(
                database_id=database_id,
                filter=filter,
                start_cursor=start_cursor,
                page_size=100,
            )
            start_cursor = response.get("next_cursor")
            has_more = response.get("has_more")
            results.extend(response.get("results"))
        return results

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query_all(self, database_id):
        """获取database中所有的数据"""
        results = []
        has_more = True
        start_cursor = None
        while has_more:
            response = self.client.databases.query(
                database_id=database_id,
                start_cursor=start_cursor,
                page_size=100,
            )
            start_cursor = response.get("next_cursor")
            has_more = response.get("has_more")
            results.extend(response.get("results"))
        return results

    def get_date_relation(self, properties, date):
        properties["年"] = get_relation(
            [
                self.get_year_relation_id(date),
            ]
        )
        properties["月"] = get_relation(
            [
                self.get_month_relation_id(date),
            ]
        )
        properties["周"] = get_relation(
            [
                self.get_week_relation_id(date),
            ]
        )
        properties["日"] = get_relation(
            [
                self.get_day_relation_id(date),
            ]
        )

