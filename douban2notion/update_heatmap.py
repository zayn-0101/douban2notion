import argparse
import os
import shutil
import time

from douban2notion.notion_helper import NotionHelper

# 产物目录：必须与 workflow 中 push 到 artifact 分支的目录保持一致
DEFAULT_ARTIFACT_DIR = "OUT_FOLDER"
# 产物所在分支：workflow 把 OUT_FOLDER 推到该分支
DEFAULT_ARTIFACT_BRANCH = "heatmap-artifacts"


def move_and_rename_file(type):
    source_path = os.path.join("./OUT_FOLDER", "notion.svg")
    if not os.path.exists(source_path):
        print(f"[heatmap] 未找到热力图源文件 {source_path}，跳过更新")
        return None
    artifact_root = os.getenv("NOTIONHUB_ARTIFACT_ROOT", DEFAULT_ARTIFACT_DIR)
    target_dir = os.path.join(artifact_root, type)
    os.makedirs(target_dir, exist_ok=True)
    new_filename = f"{int(time.time())}.svg"
    shutil.move(source_path, os.path.join(target_dir, new_filename))
    print(f"[heatmap] 产物已生成：{target_dir}/{new_filename}")
    return new_filename


def build_heatmap_url(type, filename):
    repository = os.environ["REPOSITORY"]
    branch = os.getenv("ARTIFACT_BRANCH", DEFAULT_ARTIFACT_BRANCH)
    relative_root = os.getenv(
        "NOTIONHUB_ARTIFACT_PATH", DEFAULT_ARTIFACT_DIR
    ).strip("/")
    return (
        f"https://raw.githubusercontent.com/{repository}/{branch}/"
        f"{relative_root}/{type}/{filename}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("type")
    options = parser.parse_args()
    type = options.type
    if type not in {"movie", "book"}:
        raise ValueError("免费版只支持 movie 和 book 热力图")

    notion_helper = NotionHelper(type)
    filename = move_and_rename_file(type)
    if not filename:
        return

    heatmap_url = build_heatmap_url(type, filename)
    print(f"[heatmap] URL：{heatmap_url}")

    block_id = getattr(notion_helper, "heatmap_block_id", None)
    if block_id:
        notion_helper.update_heatmap(block_id=block_id, url=heatmap_url)
        print(f"[heatmap] 已更新已有 embed 块：{block_id}")
        return

    # 页面中没有热力图块时自动创建，避免整个 run 失败
    print("[heatmap] 页面中未找到热力图 embed 块，将自动创建一个")
    notion_helper.append_blocks(
        notion_helper.page_id,
        [{"object": "block", "type": "embed", "embed": {"url": heatmap_url}}],
    )
    print("[heatmap] 已在页面末尾创建热力图 embed 块")


if __name__ == "__main__":
    main()
