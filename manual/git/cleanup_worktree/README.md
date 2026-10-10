# cleanup_worktree

マージ済みのGit worktreeとローカルブランチを、安全確認しながら対話的に
削除するための手動メンテナンスツールです。

## 必要環境

- Python 3.11 以上
- Git
- fzf
- GitHub CLI (`gh`)
- `gh auth login` 済みのGitHub認証

Python 3.11 以上を要求するのは、設定ファイルの読み込みに標準ライブラリの
`tomllib` を使用するためです。Pythonのサードパーティライブラリは不要です。

## ファイル構成

```txt
cleanup_worktree/
├── README.md
├── cleanup_worktree.py
├── cleanup_worktree_support.py
├── cleanup_worktree.example.toml
└── tests/
    └── test_cleanup_worktree.py
```

## 動作

1. 設定したルートディレクトリと、その直下のディレクトリからGit
   リポジトリを検索する
2. fzfで対象リポジトリを選択する
3. 前回選択したリポジトリを次回の候補一覧の先頭に表示する
4. fzfで削除対象のbranch / worktreeを選択する
5. GitHubとローカルGitでマージ状態を確認。未マージの場合は、
   クローズ済みPRとリモートブランチ削除を追加で検証する
6. PRタイトル・PR状態・URLと削除計画を表示し、`[Y/n]` で確認する
7. 削除完了後も削除エラー後もブランチ選択画面に戻る
8. fzfをキャンセルするとツールを終了する

## 設定

設定ファイルはデフォルトで以下を読み込みます。

```txt
~/.config/dev-tools/cleanup_worktree.toml
```

サンプルをコピーして作成できます。

```bash
mkdir -p ~/.config/dev-tools
cp manual/git/cleanup_worktree/cleanup_worktree.example.toml \
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
python3 manual/git/cleanup_worktree/cleanup_worktree.py \
  --config /path/to/cleanup_worktree.toml
```

設定ファイルが存在しない場合は警告を表示し、カレントディレクトリを
検索ルートとして使用します。

## 前回選択したリポジトリ

最後に選択したリポジトリは以下に保存します。

```txt
~/.local/state/dev-tools/cleanup_worktree.json
```

このファイルは設定ではなく実行時の状態として扱います。次回起動時に、
記録されたリポジトリが現在の検索対象に存在する場合は候補一覧の先頭に
表示します。

保存ファイルは可能な環境ではパーミッション `0600` に設定します。

## 実行

通常実行:

```bash
python3 manual/git/cleanup_worktree/cleanup_worktree.py
```

変更を行わず削除計画だけ確認する場合:

```bash
python3 manual/git/cleanup_worktree/cleanup_worktree.py --dry-run
```

## 安全方針

このツールは、安全性を優先して以下の条件を満たさない対象を削除しません。

- tracked changesがあるworktree
- non-ignored untracked filesがあるworktree
- lockedまたはstaleなworktree
- primary worktree
- default branch / primary branch
- 現在の作業ディレクトリ自身に相当するworktree
- マージ済みでなく、クローズ済みPR・同一HEAD・リモート削除を確認できないbranch
- 未マージでoriginに存在するbranch（リモート情報が取得できない場合も含む）
- マージも上記の未マージ削除条件も確認できないcommit

ignored filesのみが残っているworktreeは、削除対象を事前表示し、
確認後に `git clean -fdX` でignored filesだけを削除します。

未マージブランチの削除は、PRがCLOSEDであり、同一リポジトリのPR HEADと
ローカルHEADが一致し、origin上からブランチが削除された場合のみ許可します。
実際のリモートを `git ls-remote --exit-code` で確認し、
通信エラーやPR不明の場合は削除しません。
削除前にPRのタイトル・状態・URL、未マージ消失の警告を表示します。

削除確認プロンプトはマージ済み・未マージともに `[Y/n]` です。**Enter または `y` / `Y` で削除を承認**し、
`n` / `N` やその他の入力、入力終了（EOF）でキャンセルします。
**ignored files に `.env` などが含まれる場合も Enter で承認される**ため、
表示された削除内容を確認してから Enter を押してください。

以下の強制操作は使用しません。

- `git worktree remove --force`
- `git branch -D`
- 自動的な `git worktree prune`

削除直前にはbranch SHA、worktree HEAD、worktree状態、マージ状態を
再確認し、選択後に状態が変わっていた場合は処理を中止します。

外部コマンドはPythonの `subprocess` に引数配列として渡し、
`shell=True` は使用しません。

## テスト

```bash
python3 -m unittest discover \
  -s manual/git/cleanup_worktree/tests -p 'test_*.py' -v
```

GitHub認証なしで実行できる標準ライブラリのユニットテストです。

## Pythonコードスタイル

Google Python Style Guide の docstring 規約（モジュール、クラス、関数の説明、
必要に応じて `Args:` / `Returns:` / `Raises:`）に従います。

- https://google.github.io/styleguide/pyguide.html
