"""
UavSchedulerSim —— 面向智慧地质勘查的无人机集群并发自适应负载均衡调度仿真系统

《操作系统》课程实训项目。把企业无人机集群的四类生产级业务故障：
    ①弱网环境调度适配失效  ②异构任务资源竞争导致任务饥饿
    ③瞬时并发流量冲击导致过载  ④静态调度参数固化、资源利用率偏低
下沉映射为操作系统的线程调度、多级就绪队列、动态优先级、资源池与并发限流问题，
用 Python 原生 threading / queue 实现可复现、可对照的仿真系统。

四层映射：
    企业业务层 -> OS 原理层 -> 算法设计层 -> 代码实现层

包内模块：
    models            冻结数据模型（Task / QueueSnapshot / LinkSample / RunResult ...）
    config            配置加载（config.yaml）
    scenarios         三工况定义 + 可复现工作负载轨迹 WorkloadTrace + 容量标定
    queue_manager     两级就绪队列（多级反馈队列简化）
    link_monitor      链路状态感知与健康度评分（确定性轨迹）
    resource_limiter  资源配额与并发限流（限流稳流）
    simulator         仿真器主控（= OS 内核：无人机线程 + 线程池 + 全局时钟）
    scheduler.*       五种调度策略：fcfs / rr / priority / mlfq / dynamic
    metrics           性能指标计算
    db                MySQL 持久化（PyMySQL，可优雅降级）
    experiment        对照实验框架（3 工况 × 5 策略 = 15 组）
    visualize         matplotlib 可视化

命令行入口见项目根目录 run.py。
"""

from __future__ import annotations

__version__ = "1.1.0"
__all__ = ["__version__"]
