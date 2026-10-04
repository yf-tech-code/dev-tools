# Manual tools

このリポジトリには、人間が明示的に実行するメンテナンスツールを配置します。

- Agentから実行しない
- CIから実行しない
- Git hook等から自動実行しない
- 各ツールは実行前に内容と対象を確認する

## 必要環境

`cleanup_worktree.py` の実行には以下が必要です。

- Python 3.11 以上
- Git
- fzf
- GitHub CLI (`gh`)
- `gh auth login` 済みのGitHub認証

Python 3.11 以上を要求するのは、設定ファイルの読み込みに標準ライブラリの
`tomllib` を使用するためです。Pythonのサードパーティライブラリは不要です。

## ディレクトリ構成

```txt
dev-tools/
├── README.md
├── LICENSE
└── manual/
    └── git/
        ├── cleanup_worktree.py
        └── cleanup_worktree.example.toml
```

## cleanup_worktree.py

マージ済みのGit worktreeとローカルブランチを、安全確認しながら対話的に
削除するための手動メンテナンスツールです。

主な動作は以下です。

1. 設定したルートディレクトリと、その直下のディレクトリからGit
   リポジトリを検索する
2. fzfで対象リポジトリを選択する
3. 前回選択したリポジトリを次回の候補一覧の先頭に表示する
4. fzfで削除対象のbranch / worktreeを選択する
5. GitHubとローカルGitの状態を使ってマージ済みであることを確認する
6. 削除内容を表示し、人間の明示的な確認後に削除する
7. 削除完了後は終了せず、候補一覧を再取得して削除対象の選択画面に戻る
8. fzfをキャンセルするとツールを終了する

### 設定

設定ファイルはデフォルトで以下を読み込みます。

```txt
~/.config/dev-tools/cleanup_worktree.toml
```

サンプルをコピーして作成できます。

```bash
mkdir -p ~/.config/dev-tools
cp manual/git/cleanup_worktree.example.toml \
  ~/.config/dev-tools/cleanup_worktree.toml
```

設定例:

```toml
[cleanup_worktree]
root_directory = "~/work"
```

`root_directory` には、Gitリポジトリを配置しているルートディレクトリを
指定します。指定ディレクトリ自身と、その直下のディレクトリを検索します。

別の設定ファイルを使用する場合は `--config` で指定できます。

```bash
python3 manual/git/cleanup_worktree.py \
  --config /path/to/cleanup_worktree.toml
```

設定ファイルが存在しない場合は警告を表示し、カレントディレクトリを
検索ルートとして使用します。

### 前回選択したリポジトリ

最後に選択したリポジトリは以下に保存します。

```txt
~/.local/state/dev-tools/cleanup_worktree.json
```

このファイルは設定ではなく実行時の状態として扱います。次回起動時に、
記録されたリポジトリが現在の検索対象に存在する場合は候補一覧の先頭に
表示します。

保存ファイルは可能な環境ではパーミッション `0600` に設定します。

### 実行

通常実行:

```bash
python3 manual/git/cleanup_worktree.py
```

変更を行わず削除計画だけ確認する場合:

```bash
python3 manual/git/cleanup_worktree.py --dry-run
```

### 安全方針

このツールは、安全性を優先して以下の条件を満たさない対象を削除しません。

- tracked changesがあるworktree
- non-ignored untracked filesがあるworktree
- lockedまたはstaleなworktree
- primary worktree
- default branch / primary branch
- 現在の作業ディレクトリ自身に相当するworktree
- マージ済みであることを安全に確認できないbranch / commit

ignored filesのみが残っているworktreeは、削除対象を事前表示し、
追加の確認後に `git clean -fdX` でignored filesだけを削除します。

また、以下の強制操作は使用しません。

- `git worktree remove --force`
- `git branch -D`
- 自動的な `git worktree prune`

削除直前にはbranch SHA、worktree HEAD、worktree状態、マージ状態を
再確認し、選択後に状態が変わっていた場合は処理を中止します。

外部コマンドはPythonの `subprocess` に引数配列として渡し、
`shell=True` は使用しません。

## Pythonコードスタイル

PythonコードはGoogle Python Style Guideを参考にします。

- https://google.github.io/styleguide/pyguide.html
