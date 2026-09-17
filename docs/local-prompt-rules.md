# 提示词索引

默认分别读取以下文件，本索引不是运行时完整提示词：
1. `docs/local-keyword-blocklist.txt`：原有范围过滤，未改。
2. `docs/local-screen-triage-prompt.md`：只做范围路由；keep不是入库承诺。现有3.8初筛闸门不变。
3. `docs/local-screen-prompt.md`：keep/uncertain均先判断原材料增量，再做字段整理；kind=none过滤。0–10分仍按现有FEISHU_MIN_SCORE（默认6.0）入库，不抬门槛。
4. `docs/local-summarize-prompt.md`：缺QA时的既有fallback，改为1–5组，不凑3组。

Grok各专题仍使用`docs/local-grok-prompts/*.md`，`grok_watch._read_prompt`统一追加`docs/local-grok-quality.md`；必须经该入口运行才能加载增量约束。
`screening_value.py`只验证字段类型、枚举和有限分数，不调用模型、也不判定原文真假。
最终个性化只在final-filter按完整原稿+user-preferences判断，方法去重也只在有七天历史的这一层执行。没有新增审核模型。
