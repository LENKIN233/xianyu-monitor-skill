#!/usr/bin/env python3
"""
Xianyu Spider - 闲鱼爬虫（增强反反爬版）
使用登录状态文件绕过反爬，支持代理和请求延迟
"""
import asyncio
import json
import argparse
import random
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import sys
import re

# 尝试导入 playwright
try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print(json.dumps({"error": "playwright not installed. Run: pip install playwright && playwright install chromium"}))
    sys.exit(1)


# 用户代理池 - 模拟不同设备和浏览器
USER_AGENTS = [
    # Android Chrome
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Xiaomi 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
    # iOS Safari
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    # Android WebView
    "Mozilla/5.0 (Linux; Android 10; VOG-AL00 Build/HUAWEIVOG-AL00; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.120 Mobile Safari/537.36",
]

# 视口配置池
VIEWPORTS = [
    {"width": 412, "height": 915},   # Pixel 6
    {"width": 390, "height": 844},   # iPhone 13/14
    {"width": 360, "height": 800},   # Common Android
    {"width": 393, "height": 851},   # Pixel 5
    {"width": 428, "height": 926},   # iPhone 14 Pro Max
]


class RateLimiter:
    """请求频率限制器"""
    def __init__(self, min_delay: float = 3.0, max_delay: float = 8.0):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.last_request_time = 0
    
    async def wait(self):
        """等待随机时间，确保请求间隔"""
        elapsed = time.time() - self.last_request_time
        delay = random.uniform(self.min_delay, self.max_delay)
        
        if elapsed < delay:
            wait_time = delay - elapsed
            print(f"[RateLimit] 等待 {wait_time:.1f}s...", file=sys.stderr)
            await asyncio.sleep(wait_time)
        
        self.last_request_time = time.time()


class XianyuSpider:
    """闲鱼爬虫（支持登录状态）"""
    
    def __init__(self, state_file: Optional[str] = None, proxy: Optional[str] = None):
        self.base_url = "https://www.goofish.com"
        self.state_file = state_file
        self.proxy = proxy
        self.results = []
        self.debug = False
        self.rate_limiter = RateLimiter(min_delay=5.0, max_delay=10.0)
        self.seen_items = set()  # 去重缓存
    
    def _get_random_context(self) -> dict:
        """获取随机浏览器上下文配置"""
        ua = random.choice(USER_AGENTS)
        viewport = random.choice(VIEWPORTS)
        
        return {
            "user_agent": ua,
            "viewport": viewport,
            "device_scale_factor": random.choice([2.0, 2.5, 2.625, 3.0]),
            "is_mobile": True,
            "has_touch": True,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "permissions": ["geolocation"],
            "geolocation": {
                "longitude": random.uniform(121.4, 121.5),
                "latitude": random.uniform(31.2, 31.3)
            },
            "color_scheme": random.choice(["light", "dark"]),
        }
    
    async def search(
        self,
        keyword: str,
        max_price: Optional[int] = None,
        min_price: Optional[int] = None,
        location: Optional[str] = None,
        pages: int = 1,
        max_retries: int = 3
    ) -> List[Dict]:
        """
        搜索商品
        
        Args:
            keyword: 搜索关键词
            max_price: 最高价格
            min_price: 最低价格
            location: 地区筛选
            pages: 抓取页数
            max_retries: 最大重试次数
        
        Returns:
            商品列表
        """
        items = []
        
        for attempt in range(max_retries):
            try:
                items = await self._do_search(keyword, max_price, min_price, location, pages)
                
                # 检查是否获取到结果
                if len(items) == 0 and attempt < max_retries - 1:
                    wait_time = 5 * (attempt + 1)  # 指数退避
                    print(f"[Retry] 未获取到商品，{wait_time}s后重试... (尝试 {attempt + 1}/{max_retries})", file=sys.stderr)
                    await asyncio.sleep(wait_time)
                    continue
                
                # 成功获取结果
                break
                
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 5 * (attempt + 1)
                    print(f"[Retry] 抓取失败: {e}，{wait_time}s后重试... (尝试 {attempt + 1}/{max_retries})", file=sys.stderr)
                    await asyncio.sleep(wait_time)
                else:
                    print(f"[Error] 抓取失败，已达最大重试次数: {e}", file=sys.stderr)
        
        return items
    
    async def _do_search(
        self,
        keyword: str,
        max_price: Optional[int] = None,
        min_price: Optional[int] = None,
        location: Optional[str] = None,
        pages: int = 1
    ) -> List[Dict]:
        """执行单次搜索"""
        items = []
        
        async with async_playwright() as p:
            # 反检测启动参数
            launch_args = [
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-web-security',
                '--disable-features=IsolateOrigins,site-per-process',
                '--disable-accelerated-2d-canvas',
                '--disable-gpu',
                '--window-size=412,915',
            ]
            
            # 代理配置
            proxy_config = None
            if self.proxy:
                proxy_config = {"server": self.proxy}
                print(f"[Proxy] 使用代理: {self.proxy}", file=sys.stderr)
            
            browser = await p.chromium.launch(
                headless=True,
                args=launch_args,
                proxy=proxy_config
            )
            
            # 随机上下文配置
            context_kwargs = self._get_random_context()
            
            # 加载登录状态（关键！）
            if self.state_file and Path(self.state_file).exists():
                print(f"[Auth] 加载登录状态: {self.state_file}", file=sys.stderr)
                context_kwargs["storage_state"] = self.state_file
            else:
                print("[Warning] 未提供登录状态，可能触发反爬", file=sys.stderr)
            
            context = await browser.new_context(**context_kwargs)
            
            # 添加反检测脚本
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['zh-CN', 'zh', 'en']
                });
                // 覆盖 Canvas 指纹
                const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
                CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {
                    const imageData = originalGetImageData.call(this, x, y, w, h);
                    // 添加随机噪声
                    for (let i = 0; i < imageData.data.length; i += 4) {
                        const noise = Math.floor(Math.random() * 2);
                        imageData.data[i] = Math.min(255, imageData.data[i] + noise);
                    }
                    return imageData;
                };
            """)
            
            page = await context.new_page()
            
            # 设置响应拦截
            page.on("response", lambda response: asyncio.create_task(
                self._handle_response(response, items)
            ))
            
            try:
                for page_num in range(1, pages + 1):
                    # 频率限制
                    await self.rate_limiter.wait()
                    
                    # 构建搜索 URL
                    search_params = {"q": keyword}
                    if max_price:
                        search_params["maxPrice"] = max_price
                    if min_price:
                        search_params["minPrice"] = min_price
                    
                    from urllib.parse import urlencode
                    search_url = f"{self.base_url}/search?{urlencode(search_params)}"
                    
                    print(f"[Search] 正在抓取第 {page_num} 页...", file=sys.stderr)
                    
                    try:
                        # 访问搜索页
                        response = await page.goto(
                            search_url, 
                            wait_until="domcontentloaded", 
                            timeout=20000
                        )
                        
                        # 检查响应状态
                        if response and response.status >= 400:
                            print(f"[Error] HTTP {response.status} - 可能被反爬拦截", file=sys.stderr)
                            continue
                        
                        # 等待 API 响应（随机时间）
                        await asyncio.sleep(random.uniform(4, 7))
                        
                        # 模拟人类滚动行为
                        scroll_count = random.randint(2, 4)
                        for i in range(scroll_count):
                            scroll_amount = random.randint(300, 800)
                            await page.evaluate(f'window.scrollBy(0, {scroll_amount})')
                            await asyncio.sleep(random.uniform(0.5, 1.5))
                        
                        # 额外等待
                        await asyncio.sleep(random.uniform(2, 4))
                        
                    except PlaywrightTimeout:
                        print(f"[Timeout] 第 {page_num} 页加载超时", file=sys.stderr)
                        continue
                    except Exception as e:
                        print(f"[Error] 第 {page_num} 页抓取失败: {e}", file=sys.stderr)
                        continue
                
            except Exception as e:
                print(f"[Error] 抓取过程出错: {e}", file=sys.stderr)
            
            finally:
                await browser.close()
        
        return items
    
    async def _handle_response(self, response, items: List[Dict]):
        """拦截 API 响应"""
        try:
            url = response.url
            
            # 检查响应状态
            if response.status >= 400:
                return
            
            # 搜索列表 API
            if "h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search" in url:
                print(f"[API] 搜索列表: {url[:60]}...", file=sys.stderr)
                try:
                    data = await response.json()
                    
                    # 检查是否被拦截
                    if data.get("ret", []) and "FAIL" in str(data.get("ret")):
                        print(f"[Warning] API返回失败: {data.get('ret')}", file=sys.stderr)
                        return
                    
                    # 解析商品列表
                    result_list = data.get("data", {}).get("resultList", [])
                    for item_wrapper in result_list:
                        item = self._parse_api_item_v2(item_wrapper)
                        if item and item['id'] not in self.seen_items:
                            self.seen_items.add(item['id'])
                            items.append(item)
                    
                    print(f"[API] 获取到 {len(result_list)} 个商品，新商品 {len(items)} 个", file=sys.stderr)
                except Exception as e:
                    print(f"[Error] 解析失败: {e}", file=sys.stderr)
            
            # 商品详情 API
            elif "h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail" in url:
                print(f"[API] 商品详情", file=sys.stderr)
                
        except Exception as e:
            # 忽略解析错误
            pass
    
    def _parse_api_item_v2(self, item_wrapper: Dict) -> Optional[Dict]:
        """解析 API 返回的商品数据（与原项目兼容）"""
        try:
            # 数据结构: itemWrapper -> data -> item -> main
            item_data = item_wrapper.get("data", {}).get("item", {}).get("main", {})
            ex_content = item_data.get("exContent", {})
            click_params = item_data.get("clickParam", {}).get("args", {})
            
            if not ex_content:
                return None
            
            # 提取基本信息
            item_id = ex_content.get("itemId", "")
            title = ex_content.get("title", "")
            
            # 解析价格
            price_parts = ex_content.get("price", [])
            price_text = "".join([str(p.get("text", "")) for p in price_parts if isinstance(p, dict)])
            price = self._parse_price(price_text.replace("当前价", "").strip())
            
            # 提取卖家信息
            seller_name = ex_content.get("userNickName", "")
            
            # 提取图片
            image_url = ex_content.get("picUrl", "")
            
            # 提取位置
            location = ex_content.get("area", "")
            
            # 构建 URL
            raw_link = item_data.get("targetUrl", "")
            url = raw_link.replace("fleamarket://", "https://www.goofish.com/")
            if not url and item_id:
                url = f"{self.base_url}/item/{item_id}"
            
            # 发布时间
            pub_time_ts = click_params.get("publishTime", "")
            pub_time = ""
            if pub_time_ts and pub_time_ts.isdigit():
                from datetime import datetime
                pub_time = datetime.fromtimestamp(int(pub_time_ts)/1000).strftime("%Y-%m-%d %H:%M")
            
            # 想要人数
            wants_count = click_params.get("wantNum", "0")
            
            # 标签
            tags = []
            if click_params.get("tag") == "freeship":
                tags.append("包邮")
            r1_tags = ex_content.get("fishTags", {}).get("r1", {}).get("tagList", [])
            for tag_item in r1_tags:
                content = tag_item.get("data", {}).get("content", "")
                if "验货宝" in content:
                    tags.append("验货宝")
            
            return {
                "id": item_id,
                "title": title,
                "price": price,
                "url": url,
                "image": image_url,
                "location": location,
                "seller": seller_name,
                "publish_time": pub_time,
                "wants": wants_count,
                "tags": tags,
            }
            
        except Exception as e:
            return None
    
    def _parse_price(self, price_text: str) -> int:
        """解析价格"""
        if not price_text:
            return 0
        try:
            import re
            numbers = re.findall(r'\d+\.?\d*', price_text.replace(',', ''))
            if numbers:
                return int(float(numbers[0]))
            return 0
        except:
            return 0


def main():
    parser = argparse.ArgumentParser(description='闲鱼爬虫（增强反反爬版）')
    parser.add_argument('--keyword', '-k', required=True, help='搜索关键词')
    parser.add_argument('--max-price', type=int, help='最高价格')
    parser.add_argument('--min-price', type=int, help='最低价格')
    parser.add_argument('--location', help='地区')
    parser.add_argument('--pages', '-p', type=int, default=1, help='抓取页数')
    parser.add_argument('--state', '-s', help='登录状态文件路径（JSON）')
    parser.add_argument('--proxy', help='代理服务器（如 http://127.0.0.1:7890）')
    parser.add_argument('--debug', action='store_true', help='调试模式')
    parser.add_argument('--retries', '-r', type=int, default=3, help='最大重试次数')
    
    args = parser.parse_args()
    
    spider = XianyuSpider(state_file=args.state, proxy=args.proxy)
    spider.debug = args.debug
    
    results = asyncio.run(spider.search(
        keyword=args.keyword,
        max_price=args.max_price,
        min_price=args.min_price,
        location=args.location,
        pages=args.pages,
        max_retries=args.retries
    ))
    
    # 去重
    seen = set()
    unique_results = []
    for item in results:
        if item['id'] not in seen:
            seen.add(item['id'])
            unique_results.append(item)
    
    import sys
    output = json.dumps({
        "keyword": args.keyword,
        "count": len(unique_results),
        "items": unique_results
    }, ensure_ascii=False, indent=2)
    
    # 处理 Windows 控制台编码问题
    try:
        print(output)
    except UnicodeEncodeError:
        print(json.dumps({
            "keyword": args.keyword,
            "count": len(unique_results),
            "items": unique_results
        }, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
