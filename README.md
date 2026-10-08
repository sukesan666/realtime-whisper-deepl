# Realtime Whisper — DeepL版

Windowsで再生している英語音声を文字起こしし、日本語字幕を画面に重ねるアプリです。
[kenkiti/app-realtime-whisper](https://github.com/kenkiti/app-realtime-whisper) をベースに、翻訳と字幕表示を変更しています。

## この版の変更点

- 翻訳をArgos Translateから **DeepL API Free** に変更。
- Tauri版のオーバーレイは日本語のみ表示。白文字・黒縁・透明背景。
- 日本語字幕はNoto Sans JP、36px、太字。
- APIキーは利用者自身の `DEEPL_AUTH_KEY` 環境変数から読み込みます。

改修にはChatGPTを利用しています。元の著作権表示とMITライセンスは [LICENSE](LICENSE) に保持しています。

## データの扱い

音声認識はPC上の `faster-whisper` または `whisper.cpp` で行います。**翻訳を有効にすると、認識した英語テキストと直前の文脈がDeepLへ送信されます。完全オフラインのアプリではありません。**

DeepL APIの認証確認・翻訳にはインターネット接続が必要です。文字数上限や料金は利用者のDeepL API契約に従います。初回のライブラリ・音声認識モデルの取得にも接続が必要で、画面のフォントはGoogle Fontsから読み込みます。

設定は `config.json`、アプリログと文字起こし履歴は `logs/` にローカル保存されます。このソース配布物には、利用者の設定・ログ・認証情報・録音・スクリーンショットを含めていません。

## 必要な環境

- Windows 10 / 11。PCの再生音はWASAPI loopbackで取得します。
- Python 3.11。
- DeepL API FreeのアカウントとAPIキー（日本語翻訳を使う場合）。
- Tauri版ではNode.js / npm、RustのMSVC toolchain、Visual Studio C++ Build Tools、Microsoft Edge WebView2 Runtime。

このリポジトリはソースコード配布です。実行ファイル、依存ライブラリ、音声認識モデルは同梱していません。

## セットアップ

以下はリポジトリのルートフォルダでPowerShellを開いて実行します。

### 1. Pythonの依存関係

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` は元プロジェクトの構成を保持しており、Argos Translateも含まれます。ただし、この版で実際の翻訳を行うのはDeepLです。

### 2. DeepL APIキーを自分のPCに設定

APIキーをソースコードや設定ファイルへ書き込まず、次の入力欄へ入力します。入力中の文字は表示されません。

```powershell
$secureKey = Read-Host "DeepL APIキー" -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    [Environment]::SetEnvironmentVariable(
        "DEEPL_AUTH_KEY",
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer).Trim(),
        "User"
    )
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    $secureKey.Dispose()
}
```

設定後、PowerShellとアプリを閉じて開き直してください。この設定はPCのユーザー環境変数に保存され、リポジトリには入りません。

### 3. Tauri版をビルド

```powershell
cd app-tauri
npm.cmd ci
npm.cmd run tauri build -- --no-bundle
cd ..
powershell -ExecutionPolicy Bypass -File .\run-tauri.ps1
```

以後の起動は `run-tauri.ps1` を使います。ソースを変更した場合は上のビルドコマンドをもう一度実行してください。起動スクリプトは既存の実行ファイルを自動更新しません。

### 4. 音声認識と入力を設定

初期の設定例は元プロジェクトの `whisper.cpp` 向け設定です。モデルや実行ファイルをまだ用意していない場合は、アプリの詳細設定で次のように変更できます。

| 項目 | CPUで使う設定例 |
| --- | --- |
| Whisper backend | `faster-whisper` |
| Model | `tiny.en` |
| Device mode | `cpu` |
| Compute type | `int8` |
| Chunk seconds | `4` |
| Overlap seconds | `1.5` |

入力デバイスは、動画の音が出ているスピーカーやヘッドホンの **Loopback** を選びます。`Prepare Whisper` を押してモデルの準備を待ち、`Check DeepL en→ja` で翻訳の接続を確認します。翻訳とオーバーレイを有効にして、動画を再生しながら `Start` を押してください。

`whisper.cpp` を使う場合は、実行ファイルとモデルを自分で用意し、詳細設定でパスを指定してください。Vulkan版向けのビルド補助スクリプトは `scripts/build_whisper_cpp_portable.ps1` にあります。

## Tkinter版

Pythonだけの画面は次のコマンドで起動できます。

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

翻訳は同じDeepL実装を使います。白文字・黒縁・透明背景の字幕デザインはTauri版の変更です。

## フォルダ構成

```text
realtime_whisper/  音声取得・音声認識・DeepL翻訳・Python UI
app-tauri/        Tauri UI・字幕オーバーレイ・Rustブリッジ
scripts/          whisper.cpp用ビルド補助
config.example.json  個人情報を含まない設定例
LICENSE           元プロジェクトのMITライセンス
```

## 公開時に含めないファイル

`.gitignore` は `config.json`、`.env`、秘密鍵、`logs/`、`.state/`、`.venv/`、`node_modules/`、ビルド結果、モデルなどを除外します。GitHubのWeb画面へファイルを直接アップロードする場合は `.gitignore` が自動で除外するわけではないため、使用後の作業フォルダを丸ごとアップロードしないでください。

## ライセンス・元プロジェクト

このコードは [MIT License](LICENSE) で配布します。元の表記 `Copyright (c) 2026 Tadashi Inoue` を保持しています。

- 元プロジェクト: [kenkiti/app-realtime-whisper](https://github.com/kenkiti/app-realtime-whisper)
- 音声認識: [faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[whisper.cpp](https://github.com/ggml-org/whisper.cpp)
- UI: [Tauri](https://tauri.app/)

外部ライブラリ、モデル、フォントはそれぞれのライセンスに従います。これらの本体はこのソース配布物には同梱していません。
