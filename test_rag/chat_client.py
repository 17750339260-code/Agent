#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import json
import argparse
import requests
from pathlib import Path

# 禁用 SSL 警告（仅用于测试环境）
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ==================== 颜色输出 ====================
class Colors:
    GREEN = '\033[0;32m'
    RED = '\033[0;31m'
    NC = '\033[0m'


def print_green(msg):
    print(f"{Colors.GREEN}{msg}{Colors.NC}")


def print_red(msg):
    print(f"{Colors.RED}{msg}{Colors.NC}")


# ==================== 你的原始参数（未作任何修改）====================
DEFAULT_IP_PORT = "10.10.65.104:5030"
DEFAULT_APP_ID = 1001300033
DEFAULT_APP_SECRET = "24e74daf74124b0b96c9cb113162a976"
DEFAULT_REL_APP_ID = 597466380


def get_app_key(ip_port, app_id, app_secret):
    """第一步：获取 appKey"""
    url = f"https://{ip_port}/knowledgeService/extSecret/generateAppKey"
    headers = {'Content-Type': 'application/json'}
    payload = {
        "appId": app_id,
        "appSecret": app_secret
    }

    try:
        response = requests.post(url, headers=headers, json=payload, verify=False, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        raise Exception(f"请求 generateAppKey 失败: {e}")
    except json.JSONDecodeError as e:
        raise Exception(f"解析 JSON 响应失败: {e}")

    # 检查业务状态码（根据实际返回结构调整，常见为 code=0 或 code=200）
    code = data.get('code')
    if code is not None and code not in (0, 200):
        msg = data.get('msg') or data.get('message', '未知错误')
        raise Exception(f"获取 appKey 失败，code={code}, msg={msg}")

    # 提取 appKey（优先取 data.appKey，再取 appKey）
    app_key = data.get('resultObject', {}).get('appKey')
    if not app_key or app_key == 'null':
        raise Exception(f"未能从响应中提取 appKey，原始响应：{data}")

    return app_key


def chat(ip_port, app_id, app_key, rel_app_id, user_message):
    """第二步：调用聊天接口"""
    url = f"https://{ip_port}/knowledgeService/extChatApi/chat"
    headers = {
        'Content-Type': 'application/json',
        'appId': str(app_id),
        'appKey': app_key
    }

    payload = {
        "top_p": 1,
        "frequency_penalty": 0.01,
        "max_tokens": 8000,
        "temperature": 0.01,
        "messages": [{"content": user_message, "role": "user"}],
        "relAppId": rel_app_id
    }

    try:
        response = requests.post(url, headers=headers, json=payload, verify=False, timeout=60)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        raise Exception(f"聊天接口请求失败: {e}")
    except json.JSONDecodeError as e:
        raise Exception(f"解析聊天响应 JSON 失败: {e}")


def main():
    # 命令行参数解析
    parser = argparse.ArgumentParser(
        description='聊天客户端 - 从文件读取内容并调用聊天接口',
        epilog='示例: %(prog)s my_long_query.txt'
    )
    parser.add_argument('query_file', help='包含查询内容的文本文件')
    parser.add_argument('--ip-port', default=DEFAULT_IP_PORT,
                        help=f'IP和端口 (默认: {DEFAULT_IP_PORT})')
    parser.add_argument('--app-id', type=int, default=DEFAULT_APP_ID,
                        help=f'应用ID (默认: {DEFAULT_APP_ID})')
    parser.add_argument('--app-secret', default=DEFAULT_APP_SECRET,
                        help=f'应用密钥 (默认: {DEFAULT_APP_SECRET})')
    parser.add_argument('--rel-app-id', type=int, default=DEFAULT_REL_APP_ID,
                        help=f'关联应用ID (默认: {DEFAULT_REL_APP_ID})')

    args = parser.parse_args()

    # 检查文件是否存在
    query_file = Path(args.query_file)
    if not query_file.exists():
        print_red(f"错误: 文件 '{args.query_file}' 不存在")
        sys.exit(1)

    # 从文件中读取完整内容（保留换行符、特殊字符等）
    try:
        with open(query_file, 'r', encoding='utf-8') as f:
            user_msg = f.read()
    except Exception as e:
        print_red(f"错误: 读取文件失败 - {e}")
        sys.exit(1)

    if not user_msg or not user_msg.strip():
        print_red(f"错误: 文件 '{args.query_file}' 为空")
        sys.exit(1)

    # 显示文件大小
    file_size = query_file.stat().st_size
    print_green(f"[Step 1] 正在获取 appKey...")

    try:
        # 第一步：获取 appKey
        app_key = get_app_key(args.ip_port, args.app_id, args.app_secret)
        print_green(f"[Step 1] 成功获取 appKey: {app_key}")

        # 第二步：调用聊天接口
        print_green(f"[Step 2] 正在调用聊天接口，消息长度: {file_size} 字节")
        response = chat(args.ip_port, args.app_id, app_key, args.rel_app_id, user_msg)

        # 输出结果（美化 JSON）
        print_green("[Response]")
        print(json.dumps(response, indent=2, ensure_ascii=False))

    except Exception as e:
        print_red(f"[Error] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()