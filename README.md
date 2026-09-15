# FreeAnalyst

**Open source. Air gapped. Off at six.**

自由分析师 · 免费分析师

---

一级市场投资的尽调 agent。完全跑在你自己的机器上——你的 CIM、会议纪要、估值逻辑、
条款清单，一个字节都不离开这台电脑。

## 为什么叫 FreeAnalyst

`free` 有两个意思，两个都成立。

**自由。** 它替你把该读的材料读完、该核对的数据核对完，把时间还给你。
九点到六点，也能做完一天的尽调。

**免费。** 没有订阅，没有席位费，没有 token 账单。模型跑在你自己的机器上，
工具是开源的。一个免费的分析师，全天候待命。

## 三个承诺，三种证明方式

| 承诺 | 怎么证明 |
|---|---|
| **代码全开源** | 所有代码在 GitHub 上，任何人都可以审计 |
| **数据零出境** | 全项目只有一个网络出口（`guard.py`），只放行 `localhost`，每次调用留审计日志 |
| **结论可追溯** | 每条结论强制带原文出处；材料里没有的一律写"未提供"，禁止推测 |

第二个承诺不是口号，是可验证的：

```bash
python3 freeanalyst.py audit
```

```
  总调用   3
  放行     3  （应全部为回环地址）
  拦截     0
  外部主机 无
```

**`外部主机 无` 意味着：从第一次运行到现在，这个程序从未向你的机器之外
发起过任何一次请求。**

而 `拦截 0` 是因为目前还没有任何东西尝试出去。**想验证闸门真的拦得住，
跑 `python3 tests/test_egress_guard.py`** —— 它会拿 OpenAI、Anthropic、
Google、DeepSeek、阿里百炼的真实地址去试探，并确认全部被拦下且留痕。

```bash
$ python3 tests/test_egress_guard.py
Ran 5 tests in 0.020s

OK
```

## 快速开始

```bash
# 1. 装一个本地模型（Ollama 为例）
ollama pull qwen2.5-coder:7b

# 2. 把材料放进去（换成你自己的路径）
cp ~/deals/某个标的/*.txt corpus/

# 3. 入库
python3 freeanalyst.py ingest corpus

# 4. 提问
python3 freeanalyst.py ask "调整后 EBITDA 是怎么算的？回购条款对投资方有利吗？"

# 5. 查审计
python3 freeanalyst.py audit
```

**零第三方依赖。** 只需要 Python 3.10+ 和一个本地模型。

模型层用 OpenAI 兼容接口，不绑定后端——Ollama、LM Studio、vLLM、llama.cpp
都可以，换机器不用改代码。

## 开源框架 + 你的私有资产

这个项目刻意分成两半：

```
开源（进 git）                    私有（永久 gitignore）
├── guard.py        出网闸门      ├── root/      你填好的判断框架
├── retrieval.py    本地检索      ├── corpus/    你真实的 CIM 和纪要
├── freeanalyst.py  CLI           └── index/     你本地的索引
├── root-templates/ 空白模板
└── tests/
```

`root-templates/` 里是投资判断框架的骨架——基金身份、赛道观点、红旗清单、
筛选阈值、条款立场、备忘录模板。

**填好的版本永远不进 git。** 原因很简单：单个 deal 泄了是损失一笔生意，
那套框架泄了，基金就没有护城河了。

## 为什么机密不只是"公司名"

大多数人做保密设计，想的是"别让外人看到标的企业叫什么"，所以他们的办法是
把公司名替换成"标的企业A"再送到云端。

**这条路是错的。** 因为机密不在名字，在语义组合——收入、增速、EBITDA 率、
行业里只有三家可比标的、我们正在看其中一家。这组数字本身就是指纹。

而真正值钱的，是**你的判断框架**。所以 `root-templates/` 那套东西，
比任何单个 deal 都更需要保护。

## 926

我们相信投资的价值在于判断，不在于熬夜读材料。

九点到六点。剩下的事交给工具。

## Roadmap

- [ ] PDF / Word 解析
- [ ] 混合检索（BM25 + 向量召回）
- [ ] MCP server —— 一行配置接入 Claude Desktop / Cursor / Codex CLI
- [ ] 出稿用本地模型，对抗审查换另一个本地模型（不同模型交叉验证）

## License

MIT
