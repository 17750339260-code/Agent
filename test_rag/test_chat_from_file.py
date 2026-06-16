import json
from pathlib import Path

import pytest
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 原始参数（与 chat_from_file.sh 一致）====================
IP_PORT = "10.10.65.104:5030"
APP_ID = 1001300033
APP_SECRET = "24e74daf74124b0b96c9cb113162a976"
REL_APP_ID = 597466380

CURRENT_DIR = Path(__file__).parent
DEFAULT_QUERY_FILE = CURRENT_DIR / "word_data" / "query2.txt"


@pytest.fixture(scope="module")
def chat_config():
    """返回聊天接口的配置"""
    return {
        "ip_port": IP_PORT,
        "app_id": APP_ID,
        "app_secret": APP_SECRET,
        "rel_app_id": REL_APP_ID,
        "query_file": DEFAULT_QUERY_FILE,
    }


def read_query_file(query_file: Path) -> str:
    """从文件中读取完整内容（保留换行符、特殊字符等）"""
    if not query_file.exists():
        pytest.fail(f"错误: 文件 '{query_file}' 不存在")
    user_msg = query_file.read_text(encoding="utf-8")
    if not user_msg or not user_msg.strip():
        pytest.fail(f"错误: 文件 '{query_file}' 为空")
    return user_msg


def get_app_key(ip_port: str, app_id: int, app_secret: str) -> str:
    """第一步：获取 appKey"""
    url = f"https://{ip_port}/knowledgeService/extSecret/generateAppKey"
    headers = {"Content-Type": "application/json"}
    payload = {"appId": app_id, "appSecret": app_secret}

    try:
        response = requests.post(
            url, headers=headers, json=payload, verify=False, timeout=30
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        pytest.fail(f"[Error] 请求 generateAppKey 失败: {e}")
    except json.JSONDecodeError:
        pytest.fail(f"[Error] 解析 generateAppKey 响应 JSON 失败: {response.text}")

    code = data.get("code")
    if code is not None and code not in (0, 200):
        msg = data.get("msg") or data.get("message", "未知错误")
        pytest.fail(f"[Error] 获取 appKey 失败，code={code}, msg={msg}")

    app_key = data.get("resultObject", {}).get("appKey")
    if not app_key or app_key == "null":
        pytest.fail(f"[Error] 未能从响应中提取 appKey，原始响应：{data}")

    return app_key


def call_chat_api(
    ip_port: str, app_id: int, app_key: str, rel_app_id: int, user_message: str
) -> dict:
    """第二步：调用聊天接口"""
    url = f"https://{ip_port}/knowledgeService/extChatApi/chat"
    headers = {
        "Content-Type": "application/json",
        "appId": str(app_id),
        "appKey": app_key,
    }
    payload = {
        "top_p": 1,
        "frequency_penalty": 0.01,
        "max_tokens": 8000,
        "temperature": 0.01,
        "messages": [{"content": user_message, "role": "user"}],
        "relAppId": rel_app_id,
    }

    try:
        response = requests.post(
            url, headers=headers, json=payload, verify=False, timeout=120
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        pytest.fail(f"[Error] 聊天接口请求失败: {e}")
    except json.JSONDecodeError:
        pytest.fail(f"[Error] 解析聊天响应 JSON 失败: {response.text}")


@pytest.mark.integration
def test_chat_from_file(chat_config):
    """
    对应 chat_from_file.sh 的完整流程：
    1. 从文件读取 query
    2. 获取 appKey
    3. 调用聊天接口
    """
    query_file = Path(chat_config["query_file"])
    user_msg = read_query_file(query_file)
    file_size = query_file.stat().st_size

    print(f"[Step 1] 正在获取 appKey...")
    app_key = get_app_key(
        chat_config["ip_port"], chat_config["app_id"], chat_config["app_secret"]
    )
    print(f"[Step 1] 成功获取 appKey: {app_key}")

    print(f"[Step 2] 正在调用聊天接口，消息长度: {file_size} 字节")
    chat_response = call_chat_api(
        chat_config["ip_port"],
        chat_config["app_id"],
        app_key,
        chat_config["rel_app_id"],
        user_msg,
    )

    print("[DEBUG] 原始响应：")
    print(json.dumps(chat_response, ensure_ascii=False, indent=2))

    assert chat_response is not None
    assert isinstance(chat_response, dict)
