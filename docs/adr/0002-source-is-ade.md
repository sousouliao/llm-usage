# Source 按 ADE 归，不按计费后端拆

Codex 会话日志里的 `model_provider`（`openai` / `krill` / `custom` / `headroom`）是它当时打向哪个后端，不是用量属于谁。按这个字段分源，会把同一个 ADE 拆成好几个目录，看起来像「ADE vs 中转站」。实际口径是：用量归产生它的 ADE。Cursor 走官方接口仍是 `cursor`；Codex 无论走 Plus 还是中转站，都是 `codex`。代价是 raw 里不再保留「这次走了哪家中转站」。

中转站那一半已被 [ADR 0003](0003-codex-official-provider-only.md) 取代：中转站的调用不再采集。
