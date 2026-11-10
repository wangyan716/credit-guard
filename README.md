# credit-guard · 积分守门

WorkBuddy 的**积分（credit）消耗审计与节流技能**。给 AI 助手用的 skill：让它自己知道「哪类活儿在烧钱、该怎么省」，
同时带一个只读审计脚本，随时能算出你的真实账单结构。

> 一句话结论：**贵不在输出，在于同一份上下文被反复重付。**
> 实测一个用了 5 个月的账号，**94% 的积分只花在两个反模式上**：无人值守的定时轮询（58%）、两个开了三个月不换的会话（36%）。

---

## 它解决什么问题

| 现象 | 真实原因 | 本技能的对策 |
|---|---|---|
| 额度莫名其妙用超 | 定时任务「没东西也照跑」，每次都要重付一遍完整上下文 | R1 自动化必须先做**脚本级探测**，无变化 0 成本退出 |
| 越聊越贵 | 会话不换新，第 100 轮时输入从 47k 涨到 400k+ | R2 40 轮 / 120k 上下文就换会话，写交接单 |
| 明明只是改个文件也很贵 | 例行活用了几倍单价的高阶模型 | R3 选型表：搬运/改码用快速，取舍/成稿才用高阶 |
| 单次请求几十上百积分 | 整份 JSON / 整个仓库灌进上下文 | R4 强制 `offset/limit`、先 grep 再读 |
| 改一行跑一次，越跑越贵 | 每个工具结果都在后续每次请求里重发 | R5 合并命令、批量验证 |
| 新会话也没问啥就扣 2~3 积分 | 会话基座（系统提示 + 技能索引 + 工具 schema）约 47k token | R6 关掉不用的插件/连接器，合并碎片会话 |

## 安装

技能目录固定在 `~/.workbuddy/skills/`。用 git clone 装，升级就是 `git pull`：

```bash
# macOS / Linux
git clone https://github.com/wangyan716/credit-guard.git ~/.workbuddy/skills/credit-guard
```

```powershell
# Windows PowerShell
git clone https://github.com/wangyan716/credit-guard.git "$env:USERPROFILE\.workbuddy\skills\credit-guard"
```

没装 git 就下载 ZIP 解压，把文件夹放到 `~/.workbuddy/skills/credit-guard` 即可。
前置条件：WorkBuddy 已启动过一次（这样 `~/.workbuddy/` 才存在），以及 Python 3.8+。

装完开一个新会话，问「**积分消耗哪一类最高**」，技能会自动触发。

## 用法

```bash
# 一屏报告（只读数据库，约 1 秒）
python ~/.workbuddy/skills/credit-guard/scripts/audit.py

# 最近 30 天（回答「最近怎么这么高」用这个，别拿几个月的总量）
python ~/.workbuddy/skills/credit-guard/scripts/audit.py --days 30

# 加上「为什么贵」：基座成本、模型单价、上下文随轮次膨胀（扫 jsonl，约 7 秒）
python ~/.workbuddy/skills/credit-guard/scripts/audit.py --deep

# 存报告 / 机读 / 预算闸门（超出退出码 2）
python .../audit.py --out report.md
python .../audit.py --json
python .../audit.py --budget 50
```

示例输出（合成数据，结构与你本机一致）：

```
【哪一类最贵】
  自动化(无人值守)             5,630   占 58.1%
  超长会话(>100轮)            3,463   占 35.7%
  有人参与·常规                 607   占  6.2%
  → 这些运行里 90% 产出为零

【为什么贵（请求级结构）】
  会话基座：第 1 轮平均输入 46,928 token、2.87 积分/会话
  大输入：>120k token 的请求 529 次，占该样本 52% 积分
  缓存命中：0/1776 次请求读到缓存（0 = 每次全量重算）
  Deepseek-V4-Pro        272   42,978,030    3,369        78 积分/百万输入
  快速                    312   54,001,740      595        11
```

## 数据从哪来（只读）

| 账本 | 位置 | 用途 | 覆盖 |
|---|---|---|---|
| 账单口径 | `~/.workbuddy/workbuddy.db` → `session_usage.credit_json` | 总量 / 分类 / 排名 | 全量 |
| 结构口径 | `~/.workbuddy/projects/*/*.jsonl` → `providerData.rawUsage` | 输入输出拆分 / 模型 / 轮次 | 样本（~45%） |

**全程只读**：数据库用 `mode=ro` 打开，jsonl 只读不写，不上传任何数据。
两套账本覆盖面不同，**绝对量不能相加**，见 `references/evidence.md`。

## 目录

```
SKILL.md                  7 条硬规则（R1~R7）+ 反例清单，给模型看的行为准则
scripts/audit.py          审计引擎（纯标准库，无第三方依赖）
references/policy.md      判定树、阈值表、自动化设计模板、交接单模板、收益测算
references/evidence.md    指标算法，以及「实测 / 估算」的边界
```

## 这套东西的可信度边界

- 报告里的**输入/输出/模型/轮次结构是实测**，能自己复算；
- **基座里技能索引 ~12k、工具 schema ~22k 是按体积估算**，未逐项验证；
- **收益测算的百分比是按单价外推**，不是 A/B 实测；
- 「缓存 0 命中」是 provider 未上报、还是确实没命中，未验证 —— 按全量重算对待，是保守假设。

宁可标「未验证」，也不给一个听上去漂亮但站不住的结论。

## License

MIT
