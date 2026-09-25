# Macの自動保存とGitHub Actions

## 自動保存の対象

このリポジトリを置いたMacの作業フォルダ内で保存した変更が対象です。Macの他のフォルダ、未保存のエディター内容、Gitで除外されたファイルは送信されません。

送信先は `omoshiroproject-sketch/YUTOLU` の `main` です。現在は公開リポジトリなので、送信した内容は公開されます。

## 動作

- Macにログインして起動している間、1分ごとに変更を確認します。
- ファイルの更新が30秒以内の場合は次の確認まで待ちます。通常は編集を止めてから1〜2分程度で反映されます。
- 変更をコミットしてプッシュします。スリープ中・電源OFF中は動きません。
- `.env`や鍵などの除外設定に加え、送信前に代表的な秘密情報形式を確認します。検出値をログに出しません。
- 手動ステージング中、別ブランチ、競合・rebase中、GitHub側に未取得の変更がある場合は停止します。自動マージや強制プッシュはしません。
- 送信に失敗したコミットはMacに残り、次回に再試行します。
- GitHub側で変更が先に進んだ場合は、状態確認後に手動で取り込み・競合解消してください。

これは完全な秘密情報・個人情報の検査ではありません。公開してよいコード・資料だけを対象フォルダへ保存してください。ファイルサイズ10 MiB超、シンボリックリンク、サブモジュールは手動確認のため自動送信を止めます。

## 操作

リポジトリのルートで実行します。

```sh
# 最後の動作結果
python3 tools/autopush.py --status

# 一時停止
python3 tools/autopush.py --pause

# 再開（その場で1回確認）
python3 tools/autopush.py --resume

# 手動で1回、待機時間を省略して保存
python3 tools/autopush.py --now

# バックグラウンドサービスを停止
python3 tools/install_autopush.py --stop
```

状態・ログはローカルの `.git/yutolu-autopush/` に保存し、GitHubへは送信しません。
作業フォルダを移動した場合は、サービスの再設定が必要です。

## 別のMacへの導入

1. GitとPython 3.9以上を用意し、このリポジトリを取得する。
2. GitHub CLI等でGitの認証を設定し、Gitでプッシュできることを確認する。CodexのGitHub連携だけでは、MacのGit認証にはなりません。
3. ローカルリポジトリの `user.name` と `user.email` を設定する。
4. `python3 -m unittest discover -s tests -v` と `python3 tools/repository_guard.py --worktree` を実行する。
5. `python3 tools/install_autopush.py --install` でインストールする。これにはユーザーのLaunchAgentsフォルダへの書き込みが必要です。

認証前に仕組みだけ用意する場合は `python3 tools/install_autopush.py --install-paused` を使えます。この状態では送信せず、Git認証後に `python3 tools/autopush.py --resume` で有効化します。

## GitHub Actions

`Repository checks` は `main` へのpush、pull request、手動実行で起動します。

- 追跡中のファイルに対する代表的な秘密情報形式と禁止ファイルの検査
- 自動保存の回帰テスト（ローカルの一時リポジトリのみを使用）

Actionsの権限はリポジトリの読み取りだけです。Macのファイルを取得・プッシュする処理や、本番環境への公開は行いません。

アプリ本体は未受領のため、アプリのbuild・lint・型チェック・機能テストは含みません。
