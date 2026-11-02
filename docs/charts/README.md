# 结果图表（10 张）

本目录是 `charts/` 的一次性快照，便于在 GitHub 上直接查看结果；根目录
`charts/` 仍按 `.gitignore` 作为运行产物处理，重新跑实验时会被覆盖。

| 文件 | 来源 | 说明 |
| --- | --- | --- |
| `01-吞吐量对比.png` | `run.py all` | 3 工况 × 5 策略吞吐量 |
| `02-平均等待时延对比.png` | `run.py all` | 高 / 低优先级平均等待 |
| `03-饥饿率对比.png` | `run.py all` | 低优先级饥饿率 |
| `04-资源利用率对比.png` | `run.py all` | 资源利用率 |
| `05-业务完整率对比.png` | `run.py all` | 业务完整率 |
| `06-队列堆积时序.png` | `run.py all` | 高 / 低队列深度与系统负载 |
| `07-链路健康度与权重曲线.png` | `run.py all` | 链路健康度与动态权重 |
| `08-天气-链路健康度与指标联动.png` | `run.py weather` | 晴 / 雨 / 雪 / 沙尘对照 |
| `09-站级与MEC指标.png` | `run.py all`（伴生探针） | 站级 / MEC 指标 |
| `10-多站×多策略矩阵.png` | `run.py stations` | 18 组多站矩阵 |

重新生成：

```powershell
& ".\.venv\Scripts\python.exe" run.py all
& ".\.venv\Scripts\python.exe" run.py weather
& ".\.venv\Scripts\python.exe" run.py stations
```
