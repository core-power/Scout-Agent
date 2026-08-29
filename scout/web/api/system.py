"""系统监控 API"""
import psutil
import time
from fastapi import APIRouter
from typing import Dict, Any

router = APIRouter(prefix="/api/system")

# 网络流量统计
last_net_io = None
last_net_time = None


@router.get("/stats")
async def get_system_stats() -> Dict[str, Any]:
    """获取系统资源统计"""
    
    # CPU 信息
    cpu_percent = psutil.cpu_percent(interval=0.1)
    cpu_cores = psutil.cpu_count()
    
    # 内存信息
    memory = psutil.virtual_memory()
    memory_percent = memory.percent
    memory_used = round(memory.used / (1024**3), 2)  # GB
    memory_total = round(memory.total / (1024**3), 2)  # GB
    
    # 磁盘信息
    disk = psutil.disk_usage('/')
    disk_percent = disk.percent
    disk_used = round(disk.used / (1024**3), 2)  # GB
    disk_total = round(disk.total / (1024**3), 2)  # GB
    
    # 网络信息
    global last_net_io, last_net_time
    
    net_io = psutil.net_io_counters()
    current_time = time.time()
    
    if last_net_io and last_net_time:
        time_delta = current_time - last_net_time
        
        upload_speed = round((net_io.bytes_sent - last_net_io.bytes_sent) / time_delta / 1024, 2)  # KB/s
        download_speed = round((net_io.bytes_recv - last_net_io.bytes_recv) / time_delta / 1024, 2)  # KB/s
        
        network_speed = round(upload_speed + download_speed, 2)
    else:
        upload_speed = 0
        download_speed = 0
        network_speed = 0
    
    last_net_io = net_io
    last_net_time = current_time
    
    return {
        "cpu": {
            "percent": cpu_percent,
            "cores": cpu_cores
        },
        "memory": {
            "percent": memory_percent,
            "used": memory_used,
            "total": memory_total
        },
        "disk": {
            "percent": disk_percent,
            "used": disk_used,
            "total": disk_total
        },
        "network": {
            "speed": network_speed,
            "upload": upload_speed,
            "download": download_speed
        }
    }


@router.get("/processes")
async def get_processes() -> Dict[str, Any]:
    """获取进程列表"""
    processes = []
    
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
        try:
            info = proc.info
            # 只返回 CPU 或内存使用率较高的进程
            if info['cpu_percent'] > 1 or info['memory_percent'] > 1:
                processes.append({
                    "pid": info['pid'],
                    "name": info['name'],
                    "cpu_percent": round(info['cpu_percent'], 2),
                    "memory_percent": round(info['memory_percent'], 2),
                    "status": info['status']
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    
    # 按 CPU 使用率排序
    processes.sort(key=lambda x: x['cpu_percent'], reverse=True)
    
    return {
        "processes": processes[:20],  # 只返回前 20 个
        "total": len(processes)
    }
