# Manual tools

このリポジトリには、人間が明示的に実行するメンテナンスツールを配置します。

- Agentから実行しない
- CIから実行しない
- Git hook等から自動実行しない
- 各ツールは実行前に内容と対象を確認する
- 詳細な使い方や要件は各ツールディレクトリのREADMEに記載する

## Tools

- `manual/git/cleanup_worktree/` — マージ済みのGit worktreeとローカルブランチを安全に対話削除するツール
