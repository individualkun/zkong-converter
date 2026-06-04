# ZKONG Converter

企画特売CSVを監視フォルダから自動検知し、ファイル種類を判定して、必要な項目だけを抽出・バリデーション・API送信する変換アプリです。

## 対応ファイル

現在は、ヘッダーなしの企画特売CSV 4種類に対応しています。

- `PlanBargainMaster.CSV`: 企画特売マスタ
- `PlanBargainSubMaster.CSV`: 企画特売サブマスタ
- `PlanBargainHeadOfficeGoodsMaster.CSV`: 企画特売本部商品マスタ
- `PlanBargainStoreGoodsMaster.CSV`: 企画特売店舗商品マスタ

## 処理フロー

```text
data/waiting にCSVを配置
  -> ファイル検知
  -> data/active に移動
  -> ファイル名からレイアウト判定
  -> formats.json の定義に従ってCSV解析
  -> 必須項目チェック・型変換
  -> API送信用payload作成
  -> API送信、または dry-run
  -> data/done または data/error に移動
```

処理済みCSVはExcelで開きやすいように、`data/done` へ移動する前にUTF-8 BOM付きCSVへ変換します。

## 実行方法

```powershell
cd C:\work\zkong\zkong-converter\zkong-app
python converter.py --once --dry-run
```

監視し続ける場合:

```powershell
python converter.py --watch
```

## フォルダ

- `data/waiting`: CSV投入先
- `data/active`: 処理中ファイルの一時置き場
- `data/done`: 正常処理後のCSV
- `data/error`: エラーになったCSV
- `data/history`: API送信対象payloadの履歴JSON

## .env

設定は [zkong-app/.env](./zkong-app/.env) で行います。

```env
API_ENDPOINT=
API_TOKEN=
DRY_RUN=true
FORMAT_CONFIG=format/formats.json
WAITING_DIR=data/waiting
ACTIVE_DIR=data/active
DONE_DIR=data/done
ERROR_DIR=data/error
HISTORY_DIR=data/history
SAVE_HISTORY_JSONL=true
FILE_STABLE_SECONDS=1.0
DONE_RETENTION_DAYS=7
ERROR_RETENTION_DAYS=30
HISTORY_RETENTION_DAYS=30
MAX_DONE_FILES=100
MAX_ERROR_FILES=100
MAX_HISTORY_FILES=100
```

本番送信する場合は、`API_ENDPOINT` を設定し、`DRY_RUN=false` にします。

## formats.json の役割

[formats.json](./zkong-app/format/formats.json) は、CSVごとの読み取りルールとAPI送信用項目を定義するファイルです。

このファイルを変更することで、Pythonコードを触らずに次の調整ができます。

- ファイル名によるCSV種類の判定
- CSVの何列目を何の項目として読むか
- 必須チェック対象の変更
- 文字列、整数、小数の型変換
- APIへ送る項目、送らない項目の切り替え
- API送信用の項目名変更

## formats.json の全体構造

```json
{
  "version": 2,
  "description": "企画特売サンプルCSVに合わせたヘッダーなしCSVレイアウト定義",
  "layouts": [
    {
      "id": "plan_bargain_master",
      "name": "企画特売マスタ",
      "type": "csv",
      "has_header": false,
      "encodings": ["utf-8-sig", "cp932"],
      "detection": {
        "file_name_contains": ["PlanBargainMaster"]
      },
      "api": {
        "path": "plan-bargains"
      },
      "fields": []
    }
  ]
}
```

## layout の項目

`layouts` 配列の1要素が、1種類のCSVレイアウトを表します。

| 項目 | 説明 |
| --- | --- |
| `id` | プログラム内部で使うレイアウトID |
| `name` | ログや履歴に出す日本語名 |
| `type` | 現在は `csv` |
| `has_header` | ヘッダー行があるか。現在のサンプルCSVはヘッダーなしなので `false` |
| `encodings` | 読み込み時に試す文字コード |
| `detection.file_name_contains` | ファイル名に含まれていれば、このレイアウトと判定する文字列 |
| `api.path` | `API_ENDPOINT` の後ろに付けるAPIパス |
| `fields` | CSV列の読み取り定義 |

## fields の項目

`fields` は、CSVの各列をどう扱うかを定義します。

```json
{
  "index": 0,
  "name": "planCode",
  "label": "企画特売コード",
  "target": "planCode",
  "type": "str",
  "required": true,
  "send": true
}
```

| 項目 | 説明 |
| --- | --- |
| `index` | CSVの列番号。0始まりです。1列目は `0`、2列目は `1` |
| `name` | Python内部で使う英字の項目名 |
| `label` | 元データ上の日本語項目名。人が読むための説明 |
| `target` | APIへ送るJSONのキー名 |
| `type` | 型。`str`, `int`, `float` のいずれか |
| `required` | `true` の場合、空ならエラー |
| `send` | `true` の場合、API送信payloadに含める |

## 必要なデータだけ抜く方法

APIへ送らない項目は `send: false` にします。

```json
{
  "index": 10,
  "name": "createdBy",
  "label": "登録担当者コード",
  "target": "createdBy",
  "type": "str",
  "required": false,
  "send": false
}
```

この項目はCSVから読み取り、履歴には残せますが、API payloadには入りません。

## 必須チェックを変更する方法

必須にしたい項目は `required: true` にします。

```json
{
  "index": 3,
  "name": "itemCode",
  "label": "商品コード",
  "target": "itemCode",
  "type": "str",
  "required": true,
  "send": true
}
```

空欄だった場合、ファイルは `data/error` に移動し、同じ場所に `.error.txt` が作成されます。

## 注意点

- `index` は必ず0始まりです。
- 現在のサンプルCSVはヘッダーなしなので、列順が変わると `index` の修正が必要です。
- 金額に小数が含まれる列は `float` にしてください。
- コード値や日付は、先頭ゼロを守るため基本的に `str` にしてください。
- ファイル名判定なので、CSV名に `PlanBargainMaster` などの文字列が含まれている必要があります。

## 履歴ファイル

`SAVE_HISTORY_JSONL=true` の場合、APIへ送るpayloadを `data/history` に保存します。

現在の出力はJSON Linesではなく、1回の実行につき1つのJSONファイルです。ファイル名は次の形式です。

```text
YYYYMMDD_HHMMSS_api_payloads.json
```

中身は、処理したCSVごとに `files` 配列へまとまります。

```json
{
  "generatedAt": "2026-06-04 16:00:00",
  "dryRun": true,
  "files": [
    {
      "sourceFile": "0209_20260527024704PlanBargainMaster.CSV",
      "layoutId": "plan_bargain_master",
      "layoutName": "企画特売マスタ",
      "apiPath": "plan-bargains",
      "count": 7,
      "payloads": []
    }
  ]
}
```

`payloads` には、実際にAPIへ送る形と同じJSONが入ります。

## 自動削除

`data/done`、`data/error`、`data/history` は、使うたびにファイルが増えるため、起動時に古いファイルを自動削除します。

対象フォルダ:

- `data/done`
- `data/error`
- `data/history`

`data/waiting` と `data/active` は処理対象ファイルが入る場所なので、自動削除しません。

設定は `.env` で行います。

```env
DONE_RETENTION_DAYS=7
ERROR_RETENTION_DAYS=30
HISTORY_RETENTION_DAYS=30
MAX_DONE_FILES=100
MAX_ERROR_FILES=100
MAX_HISTORY_FILES=100
```

`*_RETENTION_DAYS` は保存日数です。`-1` にすると日数による削除をしません。

`MAX_*_FILES` は残す最大件数です。`0` にすると件数による削除をしません。
