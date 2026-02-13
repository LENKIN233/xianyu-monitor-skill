# API 参考文档

## AI Bridge API

### AIBridge 类

#### 构造函数

```python
AIBridge(workspace_dir: Optional[str] = None)
```

**参数：**
- `workspace_dir`: 工作目录，默认为系统临时目录下的 `xianyu_ai_bridge` 文件夹

#### 方法

##### create_analysis_request

```python
create_analysis_request(
    product_data: Dict,
    image_paths: List[str],
    prompt_text: str,
    task_id: Optional[str] = None
) -> str
```

创建分析请求并返回请求 ID。

**参数：**
- `product_data`: 商品数据字典
- `image_paths`: 图片文件路径列表
- `prompt_text`: 分析提示词
- `task_id`: 可选的任务 ID，用于追踪

**返回：**
- `request_id`: 请求唯一标识符

##### get_analysis_request

```python
get_analysis_request(request_id: str) -> Optional[Dict]
```

获取指定请求的数据。

**参数：**
- `request_id`: 请求 ID

**返回：**
- 请求数据字典，不存在返回 None

##### save_analysis_result

```python
save_analysis_result(request_id: str, result: Dict) -> bool
```

保存分析结果。

**参数：**
- `request_id`: 请求 ID
- `result`: 分析结果字典

**返回：**
- 是否保存成功

**结果格式示例：**
```json
{
  "prompt_version": "EagleEye-V6.4",
  "is_recommended": true,
  "reason": "卖家可信，商品描述详细，价格合理",
  "risk_tags": [],
  "criteria_analysis": {
    "model_chip": { "status": "PASS", "comment": "型号正确", "evidence": "描述中明确标注" },
    "battery_health": { "status": "PASS", "comment": "健康度良好", "evidence": "显示90%" },
    "condition": { "status": "PASS", "comment": "成色新", "evidence": "图片显示无划痕" },
    "history": { "status": "PASS", "comment": "无维修史", "evidence": "卖家声明" },
    "seller_type": { 
      "status": "PASS", 
      "persona": "个人卖家",
      "comment": "行为符合个人卖家特征",
      "analysis_details": {}
    },
    "shipping": { "status": "PASS", "comment": "包邮", "evidence": "标注包邮" },
    "seller_credit": { "status": "PASS", "comment": "信用极好", "evidence": "信用分850" }
  }
}
```

##### get_analysis_result

```python
get_analysis_result(request_id: str, timeout: int = 300) -> Optional[Dict]
```

阻塞等待并获取分析结果。

**参数：**
- `request_id`: 请求 ID
- `timeout`: 超时时间（秒），默认 300 秒

**返回：**
- 分析结果字典，超时返回 None

##### list_pending_requests

```python
list_pending_requests() -> List[str]
```

列出所有待处理的请求 ID。

**返回：**
- 请求 ID 列表

##### cancel_request

```python
cancel_request(request_id: str) -> bool
```

取消分析请求。

**参数：**
- `request_id`: 请求 ID

**返回：**
- 是否取消成功

## AIClient 类（Patched 版本）

保持与原版本兼容的接口。

### 构造函数

```python
AIClient()
```

### 方法

#### is_available

```python
is_available() -> bool
```

检查 AI 客户端是否可用。

#### analyze

```python
async analyze(
    product_data: Dict,
    image_paths: List[str],
    prompt_text: str
) -> Optional[Dict]
```

分析商品数据。

**参数：**
- `product_data`: 商品数据
- `image_paths`: 图片路径列表
- `prompt_text`: 分析提示词

**返回：**
- 分析结果字典

#### refresh

```python
refresh() -> None
```

刷新配置（兼容原接口，实际会重新初始化 AI Bridge）。

## 配置文件

### config.json

```json
{
  "ai_mode": "local_bridge",
  "bridge_workspace": "/tmp/xianyu_ai_bridge",
  "bridge_timeout": 300,
  "created_at": "2026-02-13T15:00:00"
}
```

**字段说明：**
- `ai_mode`: AI 模式，`local_bridge` 或 `openai`
- `bridge_workspace`: AI Bridge 工作目录
- `bridge_timeout`: 等待 AI 分析的超时时间（秒）

## 提示词格式

### 标准提示词结构

```
你是世界顶级的二手交易分析专家，代号 EagleEye-V6.4。

[用户自定义标准]

### 输出格式

必须返回以下 JSON 格式：
{
  "prompt_version": "EagleEye-V6.4",
  "is_recommended": boolean,
  "reason": "string",
  "risk_tags": ["string"],
  "criteria_analysis": {
    "model_chip": { "status": "PASS/FAIL/WARNING", "comment": "string", "evidence": "string" },
    "battery_health": { "status": "PASS/FAIL/WARNING", "comment": "string", "evidence": "string" },
    "condition": { "status": "PASS/FAIL/WARNING", "comment": "string", "evidence": "string" },
    "history": { "status": "PASS/FAIL/WARNING", "comment": "string", "evidence": "string" },
    "seller_type": { 
      "status": "PASS/FAIL/WARNING", 
      "persona": "string",
      "comment": "string",
      "analysis_details": {}
    },
    "shipping": { "status": "PASS/FAIL/WARNING", "comment": "string", "evidence": "string" },
    "seller_credit": { "status": "PASS/FAIL/WARNING", "comment": "string", "evidence": "string" }
  }
}
```

## 使用示例

### 作为 AI 实例分析商品

```python
from ai_bridge import get_bridge
import json

# 获取桥接实例
bridge = get_bridge()

# 获取待处理请求
pending = bridge.list_pending_requests()
for request_id in pending:
    request = bridge.get_analysis_request(request_id)
    
    # 分析商品（这里由 AI 完成）
    product_data = request['product_data']
    images = request['images_base64']
    criteria = request['prompt_text']
    
    # 构建分析结果（AI 的分析输出）
    result = {
        "prompt_version": "EagleEye-V6.4",
        "is_recommended": True,
        "reason": "...",
        "risk_tags": [],
        "criteria_analysis": { ... }
    }
    
    # 保存结果
    bridge.save_analysis_result(request_id, result)
```
