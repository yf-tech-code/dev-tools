# Manual tools

このディレクトリには、人間が明示的に実行するメンテナンスツールを配置します。

- Agentから実行しない
- CIから実行しない
- Git hook等から自動実行しない
- 各ツールは実行前に内容と対象を確認する

## ディレクトリ構成

```txt
dev-tools/
├── README.md
├── LICENSE
└── manual/
    ├── README.md
    │
    └── git/
        ├── README.md
        └── cleanup-worktree.sh
```
