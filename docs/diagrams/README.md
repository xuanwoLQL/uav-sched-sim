# 系统图册

打开 [`00-图册索引.html`](00-图册索引.html) 浏览全部 8 张图。

| 文件 | 类型 | 内容 |
|---|---|---|
| `01-系统框架图.html` | Architecture | 现场线程、调度内核、评测存储与 3D 监控台 |
| `02-数据流详细逻辑图.html` | Data flow | 任务生成、入队、调度、执行、重试、结算与发布 |
| `03-任务生命周期状态机.html` | State machine | READY / RUNNING / BLOCKED / RETRY / CACHED / DONE / FAILED / CANCELLED |
| `04-动态权重决策流程图.html` | Flowchart | 饥饿硬补偿、四因子权重、候选比较与回队 |
| `05-调度执行时序图.html` | Sequence | UAV、队列、调度器、限流器、线程池及弱网分支 |
| `06-多基站部署拓扑图.html` | Deployment | 边缘、控制面、数据面、协议与端口 |
| `07-数据库实体关系图.html` | ER | `experiment` 聚合根与四类明细表 |
| `08-实验评测流水线.html` | Process | 标定、轨迹、运行、采样、指标、持久化与报告 |

这些页面都是单文件 HTML + inline SVG，可直接浏览器打开或打印为 PDF。

重建命令：

```powershell
& ".\.venv\Scripts\python.exe" tools\build_diagrams.py
```

自检：

```powershell
$check = "D:\agent\diagram-design-main\skills\diagram-design\scripts\self_check.py"
Get-ChildItem docs\diagrams -Filter *.html | ForEach-Object {
  & ".\.venv\Scripts\python.exe" $check $_.FullName
}
```
