// =============================================================================
// replication_kernel.cu —— GPU 上并行跑「大规模重复实验」的离散事件仿真核
// =============================================================================
//
// 【为什么需要它】
//   项目 v1.1 的仿真跑在真实时钟上（2ms tick + time.sleep），一次 PEAK 工况要 9 秒墙钟。
//   想做「每个工况×策略重复几千次、给出 95% 置信区间」的正经统计研究，
//   纯 Python 需要几十小时，不可行。
//
//   但这几万次仿真之间没有任何依赖（每次是一个独立的随机实现 = 一个
//   independent replication），是天然的数据并行（SPMD）：
//       —— 一个 CUDA 线程 = 一次完整仿真（一个 replication），
//          在自己的寄存器/本地内存里跑完全部事件，最后把指标写回全局内存。
//
// 【与 Python 参考实现的关系（报告里必须写清楚）】
//   本核是分析模型（analysis model），不是参考模型的逐位复刻：
//     * 参考模型（src/uav_sim）：真实多线程 + wall-clock，高保真、可演示、可交互；
//     * 本核（analysis model）：确定性离散事件，与参考模型**同规则不同实现**。
//   一致性验证见 gpu/README.md（与参考模型对照 + 与 Python 降级路径对照）。
//
// 【已实现语义（与 src/uav_sim/scheduler/*.py 一一对应）】
//     mode 0  FCFS      跨两级队列取到达最早者，不抢占
//     mode 1  RR        同 FCFS 的取任务规则 + 固定时间片 rr_slice_ms
//     mode 2  静态优先级 高优绝对优先，不抢占（复现"低优饥饿"）
//     mode 3  动态权重   aging 硬补偿通道 + 四因子权重 + 自适应时间片 + 链路感知(本地缓存)
//     mode 4  MLFQ       aging 升权通道 + 分层时间片（level0 短 / level1 长）
//   弱网失败退避（对应 simulator.py 的 _schedule_retry）：
//     * 非链路感知策略：指数退避 base * 2^min(retry-1, 3)
//     * 链路感知策略（mode 3）且在健康度低于阈值时：改走「本地缓存」退避（固定 cache_retry_backoff_s），
//       期间任务**不在就绪队列里、不占并发槽位**（这正是痛点①的解法）
//
// 构建见 gpu/build.ps1；运行见 gpu/run_gpu.py。
// =============================================================================

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <cuda_runtime.h>

// ---------------------------------------------------------------------------
// 仿真规模上限（决定每个线程的本地内存占用）
// ---------------------------------------------------------------------------
#define MAX_TASKS   2048    // 一次 replication 最多生成的任务数（超出即截断）
#define MAX_UAV     64      // 最大无人机数
#define MAX_WORKERS 64      // 最大工作线程数（= 地面站并发处理能力）
#define MAX_PENDING 1024    // 退避重传待办表容量（对应 simulator._retry_pending）
#define MAX_COEFFS  4096    // 参数扫描的组合数上限

// ---------------------------------------------------------------------------
// 与 config.yaml 对齐的模型超参
// ---------------------------------------------------------------------------
struct ModelParams {
    int   n_uav;            // 无人机数量
    float duration;         // 仿真时长（仿真秒）
    int   workers;          // 工作线程数 = 并发容量
    int   queue_capacity;   // 就绪队列容量
    // 动态权重系数（与 dynamic.py 一一对应）
    float w0, alpha, beta, gamma, delta;
    float base_high, base_low;
    float starve_boost, starve_boost_cap;
    float weaknet_downweight;
    float cache_health_threshold;
    // 动态权重的时间片（自适应）
    float slice_high_base_ms, slice_high_min_ms, slice_high_max_ms;
    float slice_low_base_ms, slice_low_min_ms, slice_low_max_ms;
    float slice_shrink;
    float slice_preempt_threshold;
    // RR / MLFQ 的固定时间片
    float rr_slice_ms;
    float mlfq_slice0_ms, mlfq_slice1_ms, mlfq_aging_ms;
    // 系统与业务
    float starvation_ms;     // 动态权重的 aging 触发阈值（调度侧）
    float metric_starve_ms;  // 饥饿率评价阈值（业务侧）
    float retry_gain;        // 弱网失败率增益
    float retry_backoff_base_s;   // 指数退避基值（非链路感知）
    float cache_retry_backoff_s;  // 本地缓存退避（链路感知）
    float survey_gap_s;      // 勘查任务平均间隔
    float ops_gap_s;         // 运维任务平均间隔
    float health_mu;         // 链路健康度均值
    float health_sigma;      // 链路健康度波动
    float svc_high_mu_s;     // 勘查任务平均服务时长（仿真秒）
    float svc_low_mu_s;      // 运维任务平均服务时长
};

// ---------------------------------------------------------------------------
// 每次 replication 的输出指标（host 侧再聚合出均值 / 95% 置信区间）
// ---------------------------------------------------------------------------
struct Metrics {
    float throughput;         // 吞吐量（完成数 / 时长）
    float wait_high_ms;       // 高优先级平均等待
    float wait_low_ms;        // 低优先级平均等待
    float starvation_low_pct; // 低优先级饥饿率
    float completion_pct;     // 业务完整率
    float util_pct;           // 资源利用率
    float retry_pct;          // 弱网重传率
    float preempts;           // 上下文切换（被抢占次数）
    float promotions;         // aging 升权/硬补偿次数（MLFQ 与 dynamic 的防饿机制）
    float cached;             // 进入本地缓存的次数（仅链路感知策略非零）
    float max_low_depth;      // 低优队列峰值堆积
    int   submitted;          // 生成任务数
    int   done;               // 完成数
};

// ---------------------------------------------------------------------------
// 确定性 RNG（每线程一份）：xorshift64*，便宜且统计性质够用
// ---------------------------------------------------------------------------
__device__ inline unsigned long long rng_next(unsigned long long &s) {
    s ^= s >> 12; s ^= s << 25; s ^= s >> 27;
    return s * 2685821657736338717ULL;
}
__device__ inline float rng_uniform(unsigned long long &s) {
    return (float)((rng_next(s) >> 11) * (1.0 / 9007199254740992.0));
}
__device__ inline float rng_exp(unsigned long long &s, float mu) {
    float u = rng_uniform(s);
    if (u < 1e-7f) u = 1e-7f;
    return -mu * logf(u);
}
__device__ inline float clampf(float v, float lo, float hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

// 模式常量
#define MODE_FCFS      0
#define MODE_RR        1
#define MODE_PRIORITY  2
#define MODE_DYNAMIC   3
#define MODE_MLFQ      4
#define MODE_COUNT     5
__device__ __host__ inline const char *mode_name(int m) {
    switch (m) {
        case MODE_FCFS: return "fcfs";
        case MODE_RR: return "rr";
        case MODE_PRIORITY: return "priority";
        case MODE_DYNAMIC: return "dynamic";
        default: return "mlfq";
    }
}
// 链路感知：只有动态权重策略具备（对应 scheduler.link_aware）
__device__ inline bool mode_link_aware(int m) { return m == MODE_DYNAMIC; }

// ---------------------------------------------------------------------------
// 链路健康度：确定性时变曲线（同一 seed 复现同一条曲线）
// ---------------------------------------------------------------------------
__device__ inline float link_health(int uav, float t, const ModelParams &p, float phase) {
    float w = 0.11f * (float)uav + phase;
    float h = p.health_mu
            + p.health_sigma * (0.6f * sinf(0.08f * t + w)
                              + 0.4f * sinf(0.31f * t + 2.1f * w));
    return clampf(h, 0.0f, 100.0f);
}

// 入队（尾插）：对应 OS 的「就绪队列尾部插入」。用宏避免 lambda 的编译期不确定性。
#define ENQ(IDX, NOW)                                                        \
    do {                                                                     \
        enq[(IDX)] = (NOW);                                                  \
        nxt[(IDX)] = -1;                                                     \
        if (prio[(IDX)] == 1) {                                              \
            if (tail_h >= 0) nxt[tail_h] = (IDX); else head_h = (IDX);       \
            tail_h = (IDX); ++depth_h;                                       \
        } else {                                                             \
            if (tail_l >= 0) nxt[tail_l] = (IDX); else head_l = (IDX);       \
            tail_l = (IDX); ++depth_l;                                       \
            if (depth_l > (int)m.max_low_depth) m.max_low_depth = (float)depth_l; \
        }                                                                    \
    } while (0)

// ---------------------------------------------------------------------------
// 一次完整仿真（= 一个 replication）
// ---------------------------------------------------------------------------
__device__ Metrics simulate_one(int rep, int mode, const ModelParams &p) {
    Metrics m;
    m.throughput = 0.f; m.wait_high_ms = 0.f; m.wait_low_ms = 0.f;
    m.starvation_low_pct = 0.f; m.completion_pct = 0.f; m.util_pct = 0.f;
    m.retry_pct = 0.f; m.preempts = 0.f; m.promotions = 0.f; m.cached = 0.f;
    m.max_low_depth = 0.f; m.submitted = 0; m.done = 0;

    // ---- 每线程独立随机流：rep 决定种子（跨策略同 rep 同种子 = CRN） ----
    unsigned long long rng =
        0x9E3779B97F4A7C15ULL ^ (unsigned long long)(rep + 1) * 0xBF58476D1CE4E5B9ULL;
    float phase = (float)(rng_uniform(rng) * 6.2831853f);

    // ---- 任务表（本地内存，每线程一份） ----
    float svc[MAX_TASKS];         // 服务时长
    float remain[MAX_TASKS];      // 剩余服务时长
    float enq[MAX_TASKS];         // 最近一次入队时刻（等待时延记账）
    float first_start[MAX_TASKS]; // 首次获得 CPU 的时刻（-1 = 未运行过）
    float wait_acc[MAX_TASKS];    // 累计等待毫秒（跨抢占/重传累加，与 Python 口径一致）
    unsigned char prio[MAX_TASKS];    // 1 = 高优勘查，0 = 低优运维
    unsigned char uav_of[MAX_TASKS];
    unsigned char retry_of[MAX_TASKS]; // 重传次数（决定指数退避的档位）
    int nxt[MAX_TASKS];           // 队列链表指针

    int n = 0;
    const bool link_aware = mode_link_aware(mode);

    // ---- 每架无人机的两条到达流（相当于 Python 里的生产者线程） ----
    float next_high[MAX_UAV], next_low[MAX_UAV];
    for (int u = 0; u < p.n_uav; ++u) {
        next_high[u] = rng_exp(rng, p.survey_gap_s);
        next_low[u]  = rng_exp(rng, p.ops_gap_s);
    }

    // ---- 就绪队列：两级 FIFO，用「任务下标 + 链表 next[]」实现（O(1) 入队/出队） ----
    int head_h = -1, tail_h = -1, depth_h = 0;
    int head_l = -1, tail_l = -1, depth_l = 0;

    // ---- 工作线程槽位 ----
    float w_finish[MAX_WORKERS];
    int   w_task[MAX_WORKERS];
    float w_run[MAX_WORKERS];
    for (int i = 0; i < p.workers; ++i) { w_finish[i] = 0.f; w_task[i] = -1; w_run[i] = 0.f; }

    // ---- 退避待办表（对应 simulator._retry_pending）----
    // 关键：退避中的任务**不在就绪队列里**，因此不被调度器看见、也不占并发槽位。
    int   pend_idx[MAX_PENDING];
    float pend_due[MAX_PENDING];
    int   pend_n = 0;

    // ---- 累加器 ----
    double sum_wait_high = 0.0, sum_wait_low = 0.0;
    int starved_low = 0, done_high = 0, done_low = 0, retries = 0, preempts = 0;
    int promotions = 0, cached_cnt = 0, submitted_low = 0;
    double busy_service = 0.0;
    float t = 0.f;
    long guard = 0;                 // 死循环保护

    // ======================= 主循环：事件驱动 =======================
    while (t < p.duration && guard++ < 5000000L) {
        // ---------- 1) 找出最近事件（到达 / 完成 / 退避到期） ----------
        float next_arrival = 1e30f;
        for (int u = 0; u < p.n_uav; ++u) {
            if (next_high[u] < next_arrival) next_arrival = next_high[u];
            if (next_low[u]  < next_arrival) next_arrival = next_low[u];
        }
        float next_finish = 1e30f;
        for (int i = 0; i < p.workers; ++i)
            if (w_task[i] >= 0 && w_finish[i] < next_finish) next_finish = w_finish[i];
        float next_pend = 1e30f;
        for (int i = 0; i < pend_n; ++i) if (pend_due[i] < next_pend) next_pend = pend_due[i];

        float t_next = next_arrival;
        if (next_finish < t_next) t_next = next_finish;
        if (next_pend < t_next) t_next = next_pend;
        if (t_next > p.duration) t_next = p.duration;
        if (t_next > t) t = t_next;

        // ---------- 2) 退避到期 -> 回到就绪队列 ----------
        for (int i = 0; i < pend_n; ) {
            if (pend_due[i] <= t) {
                int idx = pend_idx[i];
                ENQ(idx, pend_due[i]);
                pend_idx[i] = pend_idx[--pend_n];   // 交换删除
                pend_due[i] = pend_due[pend_n];
            } else ++i;
        }

        // ---------- 3) 处理到达（生成任务并按优先级入队） ----------
        for (int u = 0; u < p.n_uav; ++u) {
            while (next_high[u] <= t && n < MAX_TASKS) {
                float arr = next_high[u];
                int idx = n++;
                prio[idx] = 1; uav_of[idx] = (unsigned char)u; retry_of[idx] = 0;
                svc[idx] = rng_exp(rng, p.svc_high_mu_s);
                remain[idx] = svc[idx];
                first_start[idx] = -1.f; wait_acc[idx] = 0.f;
                ENQ(idx, arr);
                next_high[u] = arr + rng_exp(rng, p.survey_gap_s);
            }
            while (next_low[u] <= t && n < MAX_TASKS) {
                float arr = next_low[u];
                int idx = n++;
                prio[idx] = 0; uav_of[idx] = (unsigned char)u; retry_of[idx] = 0;
                svc[idx] = rng_exp(rng, p.svc_low_mu_s);
                remain[idx] = svc[idx];
                first_start[idx] = -1.f; wait_acc[idx] = 0.f;
                ++submitted_low;
                ENQ(idx, arr);
                next_low[u] = arr + rng_exp(rng, p.ops_gap_s);
            }
        }
        m.submitted = n;

        // ---- 任务数触顶后必须"掐断到达流" ----
        // 否则 next_high/next_low 永远 <= t，最早事件恒等于"到达"，
        // 时间 t 不前进 -> 主循环空转（这里曾经把 GPU 跑成 2 分钟不返回）。
        if (n >= MAX_TASKS) {
            for (int u = 0; u < p.n_uav; ++u) { next_high[u] = 1e30f; next_low[u] = 1e30f; }
        }

        // ---------- 4) 处理完成 / 时间片到期 ----------
        for (int i = 0; i < p.workers; ++i) {
            if (w_task[i] < 0 || w_finish[i] > t + 1e-6f) continue;
            int idx = w_task[i];
            w_task[i] = -1;
            float ran = w_run[i];
            remain[idx] -= ran;
            busy_service += ran;

            if (remain[idx] > 1e-6f) {
                // 时间片耗尽仍未完成 -> 抢占，回就绪队列尾（上下文切换 +1）
                preempts++;
                ENQ(idx, t);
                continue;
            }
            // 服务完成 -> 弱网失败判定（对应 simulator.py 的 p_fail）
            float h = link_health(uav_of[idx], t, p, phase);
            float p_fail = clampf((1.f - h / 100.f) * p.retry_gain, 0.f, 1.f);
            if (rng_uniform(rng) < p_fail) {
                // 上传失败 -> 数据作废、进入退避重传（对应 _schedule_retry）
                retries++;
                int rk = (int)retry_of[idx];
                if (rk < 255) retry_of[idx] = (unsigned char)(rk + 1);
                float delay;
                if (link_aware && h < p.cache_health_threshold) {
                    // 链路感知 + 弱网 -> 本地缓存：退避更久，且不占并发槽位
                    cached_cnt++;
                    delay = p.cache_retry_backoff_s;
                } else {
                    // 非链路感知：指数退避 base x 2^min(retry-1, 3)
                    int e = rk < 3 ? rk : 3;
                    delay = p.retry_backoff_base_s * (float)(1 << e);
                }
                remain[idx] = svc[idx];
                if (pend_n < MAX_PENDING) {
                    pend_idx[pend_n] = idx; pend_due[pend_n] = t + delay; ++pend_n;
                } else {
                    ENQ(idx, t + delay);    // 待办表满 -> 退化：直接排回队尾
                }
                continue;
            }
            float wait = wait_acc[idx];          // 真正完成 -> 结算等待时延
            if (prio[idx]) { sum_wait_high += wait; done_high++; }
            else { sum_wait_low += wait; done_low++;
                   if (wait > p.metric_starve_ms) starved_low++; }
        }

        // ---------- 5) 调度：把就绪队列里的任务填进空闲槽位 ----------
        for (;;) {
            int free_w = -1;
            for (int i = 0; i < p.workers; ++i) if (w_task[i] < 0) { free_w = i; break; }
            if (free_w < 0) break;                 // 并发已满 -> 错峰（限流）
            if (head_h < 0 && head_l < 0) break;   // 队列空

            float cong = clampf((float)(depth_h + depth_l) / (float)p.queue_capacity, 0.f, 1.f);
            int pick = -1;

            if (mode == MODE_FCFS || mode == MODE_RR) {
                // FCFS / RR：不区分优先级，取「到达更早」的那个（跨队列单一 FIFO）
                if (head_h >= 0 && head_l >= 0)
                    pick = (enq[head_h] <= enq[head_l]) ? head_h : head_l;
                else
                    pick = (head_h >= 0) ? head_h : head_l;
            } else if (mode == MODE_PRIORITY) {
                // 静态优先级：高优永远优先 —— 这正是 PDF 痛点②「低优饥饿」的来源
                pick = (head_h >= 0) ? head_h : head_l;
            } else if (mode == MODE_MLFQ) {
                // ① aging 升权通道：低队列等待超过 mlfq_aging_ms -> 直接提权调度
                if (head_l >= 0 && (t - enq[head_l]) * 1000.f > p.mlfq_aging_ms) {
                    pick = head_l; promotions++;
                } else {
                    pick = (head_h >= 0) ? head_h : head_l;   // ② 分层：先 level-0 再 level-1
                }
            } else {
                // 动态权重：① 先给等待超阈值的低优任务「硬补偿通路」（防饥饿关键）
                if (head_l >= 0 && (t - enq[head_l]) * 1000.f > p.starvation_ms) {
                    pick = head_l; promotions++;
                } else {
                    float wh = -1.f, wl = -1.f;
                    if (head_h >= 0) {
                        float qh = clampf((float)depth_h / (float)p.queue_capacity, 0.f, 1.f);
                        float wf = clampf((t - enq[head_h]) * 1000.f / p.starvation_ms, 0.f, 1.f);
                        float L  = link_health(uav_of[head_h], t, p, phase) / 100.f;
                        wh = p.base_high * (p.w0 + p.alpha * qh + p.beta * wf + p.gamma * L);
                        if ((t - enq[head_h]) * 1000.f > p.starvation_ms)
                            wh *= fminf(p.starve_boost_cap, 1.f + p.starve_boost * wf);
                        if (L * 100.f < p.cache_health_threshold) wh *= p.weaknet_downweight;
                    }
                    if (head_l >= 0) {
                        float ql = clampf((float)depth_l / (float)p.queue_capacity, 0.f, 1.f);
                        float wf = clampf((t - enq[head_l]) * 1000.f / p.starvation_ms, 0.f, 1.f);
                        float L  = link_health(uav_of[head_l], t, p, phase) / 100.f;
                        wl = p.base_low * (p.w0 + p.alpha * ql + p.beta * wf + p.gamma * L);
                        if ((t - enq[head_l]) * 1000.f > p.starvation_ms)
                            wl *= fminf(p.starve_boost_cap, 1.f + p.starve_boost * wf);
                        if (L * 100.f < p.cache_health_threshold) wl *= p.weaknet_downweight;
                    }
                    pick = (wh >= wl) ? head_h : head_l;
                    if (pick < 0) pick = (head_h >= 0) ? head_h : head_l;
                }
            }
            if (pick < 0) break;

            // ---- 出队（从对应队列头部摘除） ----
            int pp = prio[pick];
            if (pp == 1) { head_h = nxt[head_h]; if (head_h < 0) tail_h = -1; --depth_h; }
            else         { head_l = nxt[head_l]; if (head_l < 0) tail_l = -1; --depth_l; }

            // ---- 等待记账：入队 -> 出队这一段是纯等待 ----
            wait_acc[pick] += fmaxf(0.f, (t - enq[pick]) * 1000.f);
            enq[pick] = t;
            if (first_start[pick] < 0.f) first_start[pick] = t;

            // ---- 时间片：各策略语义不同 ----
            float slice_ms;
            if (mode == MODE_FCFS || mode == MODE_PRIORITY) {
                slice_ms = 1e7f;                          // 不抢占，一次跑完
            } else if (mode == MODE_RR) {
                slice_ms = p.rr_slice_ms;                 // 固定时间片（不随拥堵自适应）
            } else if (mode == MODE_MLFQ) {
                // 分层时间片：level-0 短（快周转）、level-1 长（少切换），参数写死
                slice_ms = (pp == 1) ? p.mlfq_slice0_ms : p.mlfq_slice1_ms;
            } else if (pp == 1) {
                // 动态权重：HIGH 长时间片，拥堵时收缩；轻载不切分
                if (cong < p.slice_preempt_threshold) slice_ms = 1e7f;
                else slice_ms = clampf(p.slice_high_base_ms * (1.f - p.slice_shrink * cong),
                                       p.slice_high_min_ms, p.slice_high_max_ms);
            } else {
                slice_ms = clampf(p.slice_low_base_ms * (1.f - p.slice_shrink * cong),
                                  p.slice_low_min_ms, p.slice_low_max_ms);
            }
            float run = fminf(slice_ms / 1000.f, remain[pick]);
            if (run < 1e-9f) run = remain[pick];

            w_task[free_w] = pick;
            w_finish[free_w] = t + run;
            w_run[free_w] = run;
        }

        // 既没有后续到达、也没有在跑的任务、也没有待重传 -> 可以提前结束
        if (next_arrival > p.duration && next_finish > p.duration && next_pend > p.duration) break;
    }

    // ======================= 结算指标 =======================
    float dur = p.duration > 0.f ? p.duration : 1.f;
    int cap = p.workers > 0 ? p.workers : 1;
    m.done = done_high + done_low;
    m.throughput = (float)m.done / dur;
    m.wait_high_ms = done_high > 0 ? (float)(sum_wait_high / done_high) : 0.f;
    m.wait_low_ms  = done_low  > 0 ? (float)(sum_wait_low  / done_low ) : 0.f;
    int low_total = submitted_low > 0 ? submitted_low : 1;
    m.starvation_low_pct = 100.f * (float)starved_low / (float)low_total;
    int submitted = n > 0 ? n : 1;
    m.completion_pct = 100.f * (float)m.done / (float)submitted;
    m.util_pct = 100.f * (float)busy_service / (double)(dur * cap);
    m.retry_pct = 100.f * (float)retries / (float)submitted;
    m.preempts = (float)preempts;
    m.promotions = (float)promotions;
    m.cached = (float)cached_cnt;
    return m;
}

#undef ENQ

// ---------------------------------------------------------------------------
// 内核入口：每个线程跑一个 replication，把指标写回 out[rep]
// ---------------------------------------------------------------------------
__global__ void replication_kernel(int n_reps, int mode, ModelParams p, Metrics *out) {
    int rep = blockIdx.x * blockDim.x + threadIdx.x;
    if (rep >= n_reps) return;
    out[rep] = simulate_one(rep, mode, p);
}

// ---------------------------------------------------------------------------
// 参数扫描内核：gridDim.y = 第几个系数组合，x = 第几次重复
//   一次 launch 就把「n_coeffs 组系数 x n_reps 次重复」全部算完。
//   用来回答「alpha/beta/gamma/delta 取多少最好」——比 run.py sensitivity 的
//   3 组点估计强得多。
// ---------------------------------------------------------------------------
__global__ void sweep_kernel(int n_reps, int n_coeffs, ModelParams p,
                             const float4 *coeffs, const float *starve_ms, Metrics *out) {
    int rep = blockIdx.x * blockDim.x + threadIdx.x;
    int ci = blockIdx.y;
    if (rep >= n_reps || ci >= n_coeffs) return;
    ModelParams q = p;
    float4 c = coeffs[ci];
    q.alpha = c.x; q.beta = c.y; q.gamma = c.z; q.delta = c.w;
    // 可选第 5 个参数：饥饿硬补偿的触发阈值（>0 时才覆盖）
    if (starve_ms != nullptr && starve_ms[ci] > 0.f) q.starvation_ms = starve_ms[ci];
    out[(size_t)ci * n_reps + rep] = simulate_one(rep, MODE_DYNAMIC, q);
}

#ifndef UAVSIM_NO_MAIN
// ---------------------------------------------------------------------------
// 默认参数：标定到与 Python 侧 PEAK 工况同量级（load_factor 约 1.3）
// ---------------------------------------------------------------------------
static ModelParams default_params() {
    ModelParams p{};
    p.n_uav = 30; p.duration = 60.f; p.workers = 8; p.queue_capacity = 5000;
    p.w0 = 0.50f; p.alpha = 0.45f; p.beta = 0.35f; p.gamma = 0.15f; p.delta = 0.25f;
    p.base_high = 1.00f; p.base_low = 0.35f;
    p.starve_boost = 1.80f; p.starve_boost_cap = 6.00f;
    p.weaknet_downweight = 0.30f; p.cache_health_threshold = 40.f;
    p.slice_high_base_ms = 3000.f; p.slice_high_min_ms = 800.f; p.slice_high_max_ms = 6000.f;
    p.slice_low_base_ms = 400.f; p.slice_low_min_ms = 150.f; p.slice_low_max_ms = 800.f;
    p.slice_shrink = 0.60f; p.slice_preempt_threshold = 0.15f;
    p.rr_slice_ms = 500.f;
    p.mlfq_slice0_ms = 300.f; p.mlfq_slice1_ms = 800.f; p.mlfq_aging_ms = 1500.f;
    p.starvation_ms = 3000.f; p.metric_starve_ms = 15000.f;
    // 与 config.yaml 的 weaknet 段对齐：retry_gain 0.5、退避基值 0.2s；
    // 这样 p_fail = (1-health/100)*0.5，PEAK 健康度均值约 52 -> p_fail 约 0.24
    // -> 重传放大 1.32，与参考模型 docs/01 附录 A.1 的标定值一致。
    p.retry_gain = 0.5f; p.retry_backoff_base_s = 0.2f; p.cache_retry_backoff_s = 2.0f;
    // 负载构成对齐 Python 侧 PEAK 工况：**高优勘查任务几乎扛下全部负载，运维任务近乎免费**
    // （Python 侧：75 个高优任务占 98% 的字节量，2692 个低优任务只占 2%）。
    //   容量 = 8 工作线程 x 60s = 480 服务秒
    //   高优 30 机 / 24s 约 75 个 x 6.0s = 450 服务秒（占容量 94%）
    //   低优 30 机 / 1.0s 约 1800 个 x 0.01s = 18 服务秒（占容量 4%）
    //   合计 468 / 480 约 0.98，叠加弱网重传放大后落入过载区
    p.survey_gap_s = 24.0f; p.ops_gap_s = 1.0f;
    // 健康度均值对齐参考模型 PEAK 的实测 52（且 sigma=18 保证有相当比例链路跌破
    // cache_health_threshold=40，从而真正触发动态权重的「本地缓存」通路）
    p.health_mu = 52.f; p.health_sigma = 18.f;
    p.svc_high_mu_s = 7.5f; p.svc_low_mu_s = 0.01f;
    return p;
}

static const char *CSV_HEADER =
    "scheduler,coeff_index,alpha,beta,gamma,delta,starve_ms,rep,throughput,wait_high_ms,"
    "wait_low_ms,starvation_low_pct,completion_pct,util_pct,retry_pct,"
    "preempts,promotions,cached,max_low_depth,submitted,done\n";

static void dump_row(FILE *fp, const char *sched, int ci,
                     float a, float b, float g, float d, float sm, int rep, const Metrics &r) {
    fprintf(fp, "%s,%d,%.4f,%.4f,%.4f,%.4f,%.1f,%d,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,"
                "%.6f,%.6f,%.6f,%.6f,%d,%d\n",
            sched, ci, a, b, g, d, sm, rep, r.throughput, r.wait_high_ms, r.wait_low_ms,
            r.starvation_low_pct, r.completion_pct, r.util_pct, r.retry_pct,
            r.preempts, r.promotions, r.cached, r.max_low_depth, r.submitted, r.done);
}

int main(int argc, char **argv) {
    int n_reps = 2000;
    const char *out_path = nullptr;
    const char *coeff_path = nullptr;
    bool sweep = false;
    float starve_override = 0.f;    // --starve-ms：覆盖动态权重的 aging 触发阈值

    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--reps") && i + 1 < argc) n_reps = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--out") && i + 1 < argc) out_path = argv[++i];
        else if (!strcmp(argv[i], "--coeffs") && i + 1 < argc) coeff_path = argv[++i];
        else if (!strcmp(argv[i], "--starve-ms") && i + 1 < argc) starve_override = (float)atof(argv[++i]);
        else if (!strcmp(argv[i], "--sweep")) sweep = true;
        else if (argv[i][0] >= '0' && argv[i][0] <= '9') n_reps = atoi(argv[i]);  // 位置参数：重复次数
        else if (!out_path) out_path = argv[i];                                   // 位置参数：输出 CSV
    }

    ModelParams p = default_params();
    if (starve_override > 0.f) p.starvation_ms = starve_override;
    cudaDeviceProp prop{};
    cudaGetDeviceProperties(&prop, 0);
    printf("GPU: %s  sm_%d%d  SMs=%d\n", prop.name, prop.major, prop.minor,
           prop.multiProcessorCount);

    int threads = 256;
    FILE *fp = nullptr;
    if (out_path) {
        fp = fopen(out_path, "w");
        if (!fp) { printf("无法写入 %s\n", out_path); return 1; }
        fprintf(fp, "%s", CSV_HEADER);
    }

    if (sweep) {
        // ---------------- 参数扫描模式 ----------------
        float4 h_coeffs[MAX_COEFFS];
        float h_starve[MAX_COEFFS];
        int n_coeffs = 0;
        if (coeff_path) {
            FILE *cf = fopen(coeff_path, "r");
            if (!cf) { printf("无法读取系数文件 %s\n", coeff_path); return 1; }
            char line[256];
            while (fgets(line, sizeof(line), cf) && n_coeffs < MAX_COEFFS) {
                float a, b, g, d, sm = 0.f;
                int got = sscanf(line, "%f,%f,%f,%f,%f", &a, &b, &g, &d, &sm);
                if (got >= 4) {
                    h_coeffs[n_coeffs] = make_float4(a, b, g, d);
                    h_starve[n_coeffs] = (got >= 5) ? sm : 0.f;
                    n_coeffs++;
                }
            }
            fclose(cf);
        }
        if (n_coeffs == 0) {   // 没给文件 -> 用内置全因子网格兜底
            const float as[] = {0.15f, 0.30f, 0.45f, 0.60f};
            const float bs[] = {0.15f, 0.35f, 0.55f};
            const float gs[] = {0.05f, 0.15f, 0.30f};
            const float ds[] = {0.00f, 0.25f, 0.50f};
            for (float a : as) for (float b : bs) for (float g : gs) for (float d : ds)
                if (n_coeffs < MAX_COEFFS) {
                    h_coeffs[n_coeffs] = make_float4(a, b, g, d);
                    h_starve[n_coeffs] = 0.f;
                    n_coeffs++;
                }
        }
        for (int i = 0; i < n_coeffs; ++i)
            if (h_starve[i] <= 0.f) h_starve[i] = p.starvation_ms;
        printf("参数扫描：%d 组系数 x %d 次重复 = %d 次仿真\n",
               n_coeffs, n_reps, n_coeffs * n_reps);

        float4 *d_coeffs = nullptr;
        float *d_starve = nullptr;
        Metrics *d_out = nullptr;
        cudaMalloc(&d_coeffs, sizeof(float4) * n_coeffs);
        cudaMalloc(&d_starve, sizeof(float) * n_coeffs);
        cudaMemcpy(d_coeffs, h_coeffs, sizeof(float4) * n_coeffs, cudaMemcpyHostToDevice);
        cudaMemcpy(d_starve, h_starve, sizeof(float) * n_coeffs, cudaMemcpyHostToDevice);
        size_t total = (size_t)n_coeffs * n_reps;
        cudaMalloc(&d_out, sizeof(Metrics) * total);

        dim3 grid((n_reps + threads - 1) / threads, n_coeffs);
        cudaEvent_t a, b; cudaEventCreate(&a); cudaEventCreate(&b);
        cudaEventRecord(a);
        sweep_kernel<<<grid, threads>>>(n_reps, n_coeffs, p, d_coeffs, d_starve, d_out);
        cudaDeviceSynchronize();
        cudaEventRecord(b); cudaEventSynchronize(b);
        float ms = 0.f; cudaEventElapsedTime(&ms, a, b);
        printf("内核耗时 %.2f ms（%d 次仿真）\n", ms, (int)total);

        Metrics *h_out = (Metrics *)malloc(sizeof(Metrics) * total);
        cudaMemcpy(h_out, d_out, sizeof(Metrics) * total, cudaMemcpyDeviceToHost);
        if (fp) {
            for (int ci = 0; ci < n_coeffs; ++ci) {
                float4 c = h_coeffs[ci];
                for (int rep = 0; rep < n_reps; ++rep)
                    dump_row(fp, "dynamic", ci, c.x, c.y, c.z, c.w, h_starve[ci], rep,
                             h_out[(size_t)ci * n_reps + rep]);
            }
        }
        free(h_out); cudaFree(d_out); cudaFree(d_coeffs); cudaFree(d_starve);
        cudaEventDestroy(a); cudaEventDestroy(b);
        if (fp) { fclose(fp); printf("逐次结果已写入：%s\n", out_path); }
        return 0;
    }

    // ---------------- 常规模式：5 个策略各跑 n_reps 次 ----------------
    Metrics *d_out = nullptr;
    cudaMalloc(&d_out, sizeof(Metrics) * n_reps);
    Metrics *h_out = (Metrics *)malloc(sizeof(Metrics) * n_reps);

    for (int mode = 0; mode < MODE_COUNT; ++mode) {
        cudaEvent_t a, b; cudaEventCreate(&a); cudaEventCreate(&b);
        cudaEventRecord(a);
        int blocks = (n_reps + threads - 1) / threads;
        replication_kernel<<<blocks, threads>>>(n_reps, mode, p, d_out);
        cudaDeviceSynchronize();
        cudaEventRecord(b); cudaEventSynchronize(b);
        float ms = 0.f; cudaEventElapsedTime(&ms, a, b);
        cudaMemcpy(h_out, d_out, sizeof(Metrics) * n_reps, cudaMemcpyDeviceToHost);

        double th = 0, wl = 0, starv = 0, comp = 0, util = 0;
        for (int i = 0; i < n_reps; ++i) {
            th += h_out[i].throughput; wl += h_out[i].wait_low_ms;
            starv += h_out[i].starvation_low_pct; comp += h_out[i].completion_pct;
            util += h_out[i].util_pct;
        }
        printf("%-9s | %6d reps 于 %8.2f ms | 吞吐=%6.2f/s  低优时延=%8.1fms  "
               "低优饥饿=%5.1f%%  完整率=%5.1f%%  利用率=%5.1f%%\n",
               mode_name(mode), n_reps, ms, th / n_reps, wl / n_reps,
               starv / n_reps, comp / n_reps, util / n_reps);

        if (fp) {
            for (int i = 0; i < n_reps; ++i)
                dump_row(fp, mode_name(mode), -1, p.alpha, p.beta, p.gamma, p.delta,
                         p.starvation_ms, i, h_out[i]);
        }
        cudaEventDestroy(a); cudaEventDestroy(b);
    }
    if (fp) { fclose(fp); printf("逐次结果已写入：%s\n", out_path); }
    free(h_out); cudaFree(d_out);
    return 0;
}
#endif
