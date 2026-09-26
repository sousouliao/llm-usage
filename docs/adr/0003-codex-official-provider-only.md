# Codex 只采官方 provider，中转站用量不进 raw

ADR 0002 把 Codex 走中转站（`model_provider` 为 `krill` / `custom` / `tencent_codebuddy` 等）的调用也算进 `codex`。实际看下来，这部分混进了 MiMo、Claude 这类与 Codex 订阅无关的模型，还有模型名对不上公开牌价的调用，卡片上想表达的「我在 Codex 订阅上的消耗」被稀释了。现在只保留 `model_provider=openai`，其余调用在采集时丢弃。Source 仍然按 ADE 归，ADR 0002 的这一半不变；变的只是中转站那部分不再采集。

采集的负责范围按日志里全部 provider 的最早一天算，所以旧 raw 里残留的中转站数据会在各机器下一次重采时被覆盖掉，不需要手工清理。代价是：中转站的历史用量从此不在仓库里，想恢复只能改回来重采本机日志。
