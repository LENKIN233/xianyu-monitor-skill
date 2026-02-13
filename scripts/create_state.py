#!/usr/bin/env python3
"""
登录状态提取助手 - 生成Playwright可用的storage_state格式
"""
import json
import argparse
from pathlib import Path


def create_storage_state(cookie_string: str, output_file: str):
    """
    从Cookie字符串创建Playwright storage_state文件
    
    Args:
        cookie_string: 从浏览器复制的cookie字符串 (name=value; name2=value2)
        output_file: 输出文件路径
    """
    cookies = []
    
    # 解析cookie字符串
    for item in cookie_string.split(';'):
        item = item.strip()
        if '=' in item:
            name, value = item.split('=', 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".goofish.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "None"
            })
    
    storage_state = {
        "cookies": cookies,
        "origins": []
    }
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(storage_state, f, ensure_ascii=False, indent=2)
    
    print(f"登录状态已保存到: {output_file}")
    print(f"共 {len(cookies)} 个cookie")


def main():
    parser = argparse.ArgumentParser(description='创建Playwright登录状态文件')
    parser.add_argument('--cookie', '-c', required=True, help='Cookie字符串')
    parser.add_argument('--output', '-o', default='state.json', help='输出文件')
    
    args = parser.parse_args()
    
    create_storage_state(args.cookie, args.output)


if __name__ == "__main__":
    main()
