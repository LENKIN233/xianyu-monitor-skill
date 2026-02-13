#!/usr/bin/env python3
"""
Task Manager - 监控任务管理器
简单的 JSON 文件存储，供 AI 调用
"""
import json
import os
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional


class TaskManager:
    """任务管理器"""
    
    def __init__(self, data_file: str = "tasks.json"):
        self.data_file = Path(data_file)
        self.tasks = []
        self._load()
    
    def _load(self):
        """加载任务列表"""
        if self.data_file.exists():
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.tasks = data.get('tasks', [])
            except:
                self.tasks = []
        else:
            self.tasks = []
    
    def _save(self):
        """保存任务列表"""
        with open(self.data_file, 'w', encoding='utf-8') as f:
            json.dump({
                'tasks': self.tasks,
                'updated_at': datetime.now().isoformat()
            }, f, ensure_ascii=False, indent=2)
    
    def _find_existing_task(self, keyword: str, max_price: Optional[int], min_price: Optional[int]) -> Optional[Dict]:
        """查找是否已存在相同条件的任务"""
        for task in self.tasks:
            if (task['keyword'] == keyword and 
                task['max_price'] == max_price and 
                task['min_price'] == min_price and
                task['status'] == 'running'):
                return task
        return None
    
    def create_task(
        self,
        keyword: str,
        max_price: Optional[int] = None,
        min_price: Optional[int] = None,
        criteria: str = "",
        location: Optional[str] = None,
        notification_channel: str = "feishu",
        skip_duplicate: bool = True
    ) -> Dict:
        """创建新任务
        
        Args:
            skip_duplicate: 如果存在相同条件的任务，是否跳过创建
        """
        # 参数验证
        if max_price is not None and min_price is not None and max_price < min_price:
            raise ValueError("最高价格不能低于最低价格")
        
        if max_price is not None and max_price < 0:
            raise ValueError("价格不能为负数")
        
        # 检查重复任务
        if skip_duplicate:
            existing = self._find_existing_task(keyword, max_price, min_price)
            if existing:
                return {
                    'id': existing['id'],
                    'keyword': existing['keyword'],
                    'status': 'existing',
                    'message': '相同条件的任务已存在',
                    'created_at': existing['created_at']
                }
        
        task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        task = {
            'id': task_id,
            'keyword': keyword,
            'max_price': max_price,
            'min_price': min_price,
            'criteria': criteria,
            'location': location,
            'notification': {
                'channel': notification_channel,
                'enabled': True
            },
            'status': 'running',
            'created_at': datetime.now().isoformat(),
            'last_run': None,
            'results_count': 0,
            'last_results': []  # 添加最后检查结果记录
        }
        
        self.tasks.append(task)
        self._save()
        
        return task
    
    def get_task(self, task_id: str) -> Optional[Dict]:
        """获取指定任务"""
        for task in self.tasks:
            if task['id'] == task_id:
                return task
        return None
    
    def get_running_tasks(self) -> List[Dict]:
        """获取运行中的任务"""
        return [t for t in self.tasks if t['status'] == 'running']
    
    def list_tasks(self) -> List[Dict]:
        """列出所有任务"""
        return self.tasks
    
    def stop_task(self, task_id: str) -> bool:
        """停止任务"""
        for task in self.tasks:
            if task['id'] == task_id:
                task['status'] = 'stopped'
                self._save()
                return True
        return False
    
    def delete_task(self, task_id: str) -> bool:
        """删除任务"""
        for i, task in enumerate(self.tasks):
            if task['id'] == task_id:
                self.tasks.pop(i)
                self._save()
                return True
        return False
    
    def update_last_run(self, task_id: str, results_count: int = 0):
        """更新最后运行时间"""
        for task in self.tasks:
            if task['id'] == task_id:
                task['last_run'] = datetime.now().isoformat()
                task['results_count'] += results_count
                self._save()
                break


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='任务管理器')
    parser.add_argument('--list', action='store_true', help='列出所有任务')
    parser.add_argument('--create', help='创建任务（关键词）')
    parser.add_argument('--max-price', type=int, help='最高价格')
    parser.add_argument('--min-price', type=int, help='最低价格')
    parser.add_argument('--criteria', default='', help='筛选标准')
    parser.add_argument('--stop', help='停止任务')
    parser.add_argument('--delete', help='删除任务')
    parser.add_argument('--running', action='store_true', help='列出运行中任务')
    
    args = parser.parse_args()
    
    manager = TaskManager()
    
    if args.create:
        task = manager.create_task(
            keyword=args.create,
            max_price=args.max_price,
            min_price=args.min_price,
            criteria=args.criteria
        )
        print(json.dumps(task, ensure_ascii=False, indent=2))
    
    elif args.list:
        tasks = manager.list_tasks()
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
    
    elif args.running:
        tasks = manager.get_running_tasks()
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
    
    elif args.stop:
        if manager.stop_task(args.stop):
            print(f"任务 {args.stop} 已停止")
        else:
            print(f"任务 {args.stop} 不存在")
    
    elif args.delete:
        if manager.delete_task(args.delete):
            print(f"任务 {args.delete} 已删除")
        else:
            print(f"任务 {args.delete} 不存在")
    
    else:
        print("请指定操作：--create, --list, --running, --stop, --delete")


if __name__ == "__main__":
    main()
