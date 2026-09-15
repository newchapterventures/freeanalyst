# root-templates —— 你的投资判断框架

## 这是什么

这十二个文件是**基金本身**。

单个 deal 的资料泄了，损失一笔生意。这套框架泄了，基金就没有护城河了——
因为它记录的不是"我们看了什么"，而是**"我们凭什么做判断"**。

## 怎么用

```bash
mkdir -p root
cp root-templates/firm-brain.md root/
cp root-templates/sector-theses.md root/
# ... 其余同理
```

**`root/` 已经在 `.gitignore` 里。填好的版本永远不进 git。**

`root-templates/` 里的空白版本留在仓库里，别人可以拿去改自己的。

## 文件清单

| 文件 | 装什么 | 为什么关键 |
|---|---|---|
| `firm-brain.md` | 基金身份、投资范围、不可妥协的事 | agent 的每一条输出都要对齐它 |
| `sector-theses.md` | 每个你投的赛道，一页观点 | 决定机会怎么筛 |
| `red-flags.md` | 一票否决清单，按类别 | 决定什么直接毙掉 |
| `target-metrics.md` | 规模、估值、增速、毛利阈值 | 决定初筛过不过 |
| `management-scoring.md` | 评估创始人和 CEO 的量表 | 人是最重要的变量 |
| `legal-standard-terms.md` | 你对 NDA / 回购 / 对赌的标准立场 | 判断条款偏离市场惯例 |
| `covenant-library.md` | 历史上见过的失败条款 | 前车之鉴 |
| `evidence-rules.md` | 什么算证据、什么只是口径 | 防止把管理层说法当事实 |
| `memo-template.md` | 投资备忘录的结构 | 让输出能直接进投委会 |
| `deal-log.md` | 看过的项目与放弃的理由 | 对比新旧项目，避免重复踩坑 |
| `dd-checklist.md` | 尽调清单（按阶段 + 行业） | 不漏项 |
| `portfolio-kpis.md` | 投后要盯的指标，精确定义 | 从投前到投后一致 |

## 一个提醒

填这些文件的时候，**别写"原则"式的空话**。

不要写"我们偏好优秀的团队"。要写"CEO 有至少一个完整周期
（从 0 到 1000 万收入）的操盘经历；没有的，除非有强技术壁垒否则不投"。

agent 只能执行可判定的规则。模糊的偏好等于没有偏好——
它会自己编一套来填空白，而那套不是你的。
