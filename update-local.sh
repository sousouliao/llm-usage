#!/usr/bin/env bash
# 本机用量更新脚本：同步 → 采集 → 写原始数据 → 推送。
#
# 采集必须在本机跑：Cursor 要用本机登录态，Codex 要读 ~/.codex 会话日志，
# Antigravity 要读 ~/.gemini/antigravity/conversations 会话库，云端 runner 都拿不到。本脚本只推送「原始数据」，产物交给 GitHub Actions。
#
# 用法（仓库根目录）：
#   ./update-local.sh
#   SINCE=2026-07-01 ./update-local.sh     # 临时改采集起点
#
# 前置：
#   - Python 3.12+，已装依赖（python3 -m venv .venv && .venv/bin/pip install -r requirements.txt）
#   - 已从 config/sources.example.yaml 复制出 sources.yaml
#   - 本机登录着 Cursor（或设置了 CURSOR_SESSION_TOKEN）
#   - DeepSeek：把 DEEPSEEK_PLATFORM_TOKEN 写在仓库根的 .env（已 gitignore）。
#     launchd 不会带上交互式 shell 的环境变量，所以不能只 export 在 zshrc 里。
#
# 每次都重采 [起点, 今天] 全量并覆盖写回，所以采集幂等：漏跑几天补跑一次即可，
# 不会产生重复记录，也不会因为漏跑而永久丢数据（用量历史在 Cursor 账号侧）。
set -euo pipefail
cd "$(dirname "$0")"

# launchd / cron 的 PATH 很窄。本机 git 和 python3 都在 Homebrew 前缀下。
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
export GIT_TERMINAL_PROMPT=0
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

# 本机密钥（DeepSeek userToken 等）。launchd 跑这个脚本时读这里，不写进 plist。
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "错误：未找到 python3，也没有 .venv" >&2
  exit 1
fi

# 本机会写出但不该带进合并的两类文件：
#
# 产物（stats.json / SVG）归 GitHub Actions 独占。本机现在用 --collect-only，
# 不会再写它们；但一次手跑完整 run.py 仍可能留下脏产物。raw 则是每次全量
# 重采的结果，两台机器写同一个账号级文件。带着这两类脏文件去同步，就会和
# 远端撞车，而冲突一旦留下，后面每小时都会卡在「未合并文件」上。两类文件
# 都能重新生成，丢弃是安全的。
CI_OWNED_PATHS=(data/stats.json assets)
GENERATED_PATHS=("${CI_OWNED_PATHS[@]}" data/raw)
COMMIT_MSG="chore(data): usage raw @ $(date +%F)"

restore_generated() {
  git checkout -f HEAD -- "${GENERATED_PATHS[@]}" 2>/dev/null || true
}

unmerged_files() {
  git diff --name-only --diff-filter=U
}

collect() {
  if [ -n "${SINCE:-}" ]; then
    "$PY" run.py --collect-only --since "$SINCE"
  else
    "$PY" run.py --collect-only
  fi
  # --collect-only 不写产物；这里只清掉一次完整 run.py 留下的脏文件。
  git checkout -f HEAD -- "${CI_OWNED_PATHS[@]}" 2>/dev/null || true
}

commit_raw() {  # 有变更则提交，返回 0 表示有提交
  git add data/raw
  if git diff --cached --quiet; then
    return 1
  fi
  git commit -m "$COMMIT_MSG"
}

# 上一轮若在冲突里退出，先自愈，不要求人工介入。
git rebase --abort 2>/dev/null || true
restore_generated
while git stash list | grep -q '^stash@{0}: autostash$'; do
  echo "==> 丢弃上次同步残留的贮藏"
  git stash drop >/dev/null
done
if [ -n "$(unmerged_files)" ]; then
  echo "错误：仍有未解决的冲突，需要手工处理：" >&2
  unmerged_files >&2
  exit 1
fi

# 先同步再采集。此刻 raw 与产物都和 HEAD 一致，autostash 里不会有它们，
# 也就没有和远端同一个文件撞车的机会。
echo "==> 同步远端"
git pull --rebase --autostash origin main

echo "==> 采集原始数据"
collect

echo "==> 提交原始数据"
if ! commit_raw; then
  echo "无变更，跳过提交。"
  exit 0
fi

if ! git push origin main; then
  # 远端在采集期间前进了（CI 回写产物，或另一台机器推了 raw）。不需要合并：
  # raw 是全量重采的结果，基于最新远端再采一次就是完整的最新状态。
  echo "==> 推送被拒，基于最新远端重采一次"
  git fetch origin main
  if [ "$(git rev-list --count FETCH_HEAD..HEAD)" != "1" ] ||
     [ -n "$(git status --porcelain -- ':(exclude)data/raw')" ]; then
    echo "错误：本地还有其他未推送的提交或改动，不做自动重来。" >&2
    exit 1
  fi
  git reset --hard FETCH_HEAD
  collect
  if ! commit_raw; then
    echo "无变更，跳过提交。"
    exit 0
  fi
  git push origin main
fi

echo "==> 已推送。GitHub Actions 会重新生成 stats.json 与 SVG，README 随之刷新。"
