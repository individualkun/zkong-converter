from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from watchfiles import Change, watch
except ModuleNotFoundError:
    Change = None
    watch = None


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "format" / "formats.json"


def env_path(name: str, default: str) -> Path:
    """環境変数で指定されたパスを取得し、相対パスなら zkong-app 基準に直す。"""
    path = Path(os.getenv(name, default)).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def env_int(name: str, default: int) -> int:
    """整数の環境変数を読む。未設定や不正値ならdefaultを使う。"""
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class AppSettings:
    """アプリ全体で使う設定値をまとめて持つ入れ物。"""
    waiting_dir: Path
    active_dir: Path
    done_dir: Path
    error_dir: Path
    history_dir: Path
    config_path: Path
    api_endpoint: str
    api_token: str
    dry_run: bool
    save_history_jsonl: bool
    file_stable_seconds: float
    done_retention_days: int
    error_retention_days: int
    history_retention_days: int
    max_done_files: int
    max_error_files: int
    max_history_files: int

    @classmethod
    def from_env(cls, dry_run: bool = False) -> "AppSettings":
        """`.env` とOS環境変数を読み込み、実行時設定を作る。"""
        load_dotenv_file(BASE_DIR / ".env")
        return cls(
            waiting_dir=env_path("WAITING_DIR", str(BASE_DIR / "data" / "waiting")),
            active_dir=env_path("ACTIVE_DIR", str(BASE_DIR / "data" / "active")),
            done_dir=env_path("DONE_DIR", str(BASE_DIR / "data" / "done")),
            error_dir=env_path("ERROR_DIR", str(BASE_DIR / "data" / "error")),
            history_dir=env_path("HISTORY_DIR", str(BASE_DIR / "data" / "history")),
            config_path=env_path("FORMAT_CONFIG", str(DEFAULT_CONFIG_PATH)),
            api_endpoint=os.getenv("API_ENDPOINT", "").strip(),
            api_token=os.getenv("API_TOKEN", "").strip(),
            dry_run=dry_run or os.getenv("DRY_RUN", "false").lower() == "true",
            save_history_jsonl=os.getenv("SAVE_HISTORY_JSONL", "true").lower() == "true",
            file_stable_seconds=float(os.getenv("FILE_STABLE_SECONDS", "1.0")),
            done_retention_days=env_int("DONE_RETENTION_DAYS", 7),
            error_retention_days=env_int("ERROR_RETENTION_DAYS", 30),
            history_retention_days=env_int("HISTORY_RETENTION_DAYS", 30),
            max_done_files=env_int("MAX_DONE_FILES", 100),
            max_error_files=env_int("MAX_ERROR_FILES", 100),
            max_history_files=env_int("MAX_HISTORY_FILES", 100),
        )

    def ensure_dirs(self) -> None:
        """処理に必要なフォルダが無ければ作成する。"""
        for directory in [
            self.waiting_dir,
            self.active_dir,
            self.done_dir,
            self.error_dir,
            self.history_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)


class LayoutCatalog:
    """レイアウト定義ファイルを読み込み、届いたファイルの種類を判定する。"""

    def __init__(self, config_path: Path):
        """formats.json を読み込み、レイアウト一覧を保持する。"""
        with config_path.open("r", encoding="utf-8") as file:
            self.config = json.load(file)
        self.layouts = self.config["layouts"]

    def detect(self, file_path: Path) -> dict[str, Any] | None:
        """ファイル名とCSVヘッダーを見て、最も一致度が高いレイアウトを返す。"""
        sample_headers = read_csv_headers(file_path)
        upper_name = file_path.name.upper()

        best_layout: dict[str, Any] | None = None
        best_score = 0
        for layout in self.layouts:
            detection = layout.get("detection", {})
            score = 0

            for pattern in detection.get("file_name_contains", []):
                if pattern.upper() in upper_name:
                    score += 10

            required_headers = set(detection.get("required_headers", []))
            if required_headers and required_headers.issubset(set(sample_headers)):
                score += 100 + len(required_headers)

            if score > best_score:
                best_layout = layout
                best_score = score

        return best_layout


def read_csv_headers(file_path: Path) -> list[str]:
    """CSVの1行目をヘッダーとして読み取る。文字コードはUTF-8とCP932を順に試す。"""
    for encoding in ("utf-8-sig", "cp932"):
        try:
            with file_path.open("r", encoding=encoding, newline="") as file:
                reader = csv.reader(file)
                return [header.strip() for header in next(reader, [])]
        except UnicodeDecodeError:
            continue
    return []


class CsvLayoutParser:
    """レイアウト定義に従ってCSVを内部データへ変換する。"""

    def parse(self, file_path: Path, layout: dict[str, Any]) -> list[dict[str, Any]]:
        """レイアウトに書かれた文字コード候補を順に試してCSVを読み込む。"""
        for encoding in layout.get("encodings", ["utf-8-sig", "cp932"]):
            try:
                return self._parse_with_encoding(file_path, layout, encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError(f"CSVの文字コードを判定できません: {file_path.name}")

    def _parse_with_encoding(
        self, file_path: Path, layout: dict[str, Any], encoding: str
    ) -> list[dict[str, Any]]:
        """指定された文字コードでCSVを開き、全行を変換する。"""
        if layout.get("has_header", True) is False:
            return self._parse_without_header(file_path, layout, encoding)

        with file_path.open("r", encoding=encoding, newline="") as file:
            reader = csv.DictReader(file)
            rows = []
            for line_no, raw_row in enumerate(reader, start=2):
                normalized = {
                    key.strip(): (value or "").strip()
                    for key, value in raw_row.items()
                    if key is not None
                }
                rows.append(self._convert_row(normalized, layout, line_no))
            return rows

    def _parse_without_header(
        self, file_path: Path, layout: dict[str, Any], encoding: str
    ) -> list[dict[str, Any]]:
        """ヘッダーなしCSVを列番号ベースで読み込む。"""
        with file_path.open("r", encoding=encoding, newline="") as file:
            reader = csv.reader(file)
            rows = []
            for line_no, raw_row in enumerate(reader, start=1):
                if not any((value or "").strip() for value in raw_row):
                    continue
                rows.append(self._convert_index_row(raw_row, layout, line_no))
            return rows

    def _convert_row(
        self, raw_row: dict[str, str], layout: dict[str, Any], line_no: int
    ) -> dict[str, Any]:
        """CSVの日本語列名から、プログラム内部で扱う英字キーへ変換する。"""
        converted: dict[str, Any] = {"_line_no": line_no}
        for field in layout["fields"]:
            source = field["source"]
            value = raw_row.get(source, "")
            converted[field["name"]] = convert_value(value, field.get("type", "str"))
        return converted

    def _convert_index_row(
        self, raw_row: list[str], layout: dict[str, Any], line_no: int
    ) -> dict[str, Any]:
        """列番号指定のレイアウトに従って、CSV行を内部データへ変換する。"""
        converted: dict[str, Any] = {"_line_no": line_no}
        for field in layout["fields"]:
            index = field["index"]
            value = raw_row[index].strip() if index < len(raw_row) else ""
            converted[field["name"]] = convert_value(value, field.get("type", "str"))
        return converted


def convert_value(value: str, field_type: str) -> Any:
    """レイアウト定義の型に合わせて、文字列をint/float/strへ変換する。"""
    if value == "":
        return None
    if field_type == "int":
        return int(value.replace(",", ""))
    if field_type == "float":
        return float(value.replace(",", ""))
    return value


class RowValidator:
    """変換済みデータが送信してよい内容かチェックする。"""

    def validate(self, rows: list[dict[str, Any]], layout: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        """必須項目が空でないか確認し、正常行とエラー一覧に分ける。"""
        errors: list[str] = []
        valid_rows: list[dict[str, Any]] = []
        required_fields = [field["name"] for field in layout["fields"] if field.get("required")]

        for row in rows:
            row_errors = []
            for field_name in required_fields:
                if row.get(field_name) in (None, ""):
                    row_errors.append(f"{field_name} is required")

            if row_errors:
                errors.append(f"line {row.get('_line_no')}: {', '.join(row_errors)}")
            else:
                valid_rows.append(row)

        return valid_rows, errors


class ApiClient:
    """変換済みデータを外部APIへ送信するための薄いクライアント。"""

    def __init__(self, settings: AppSettings):
        """API送信に必要な設定を保持する。"""
        self.settings = settings

    def send(self, layout: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        """レイアウト定義に沿ってpayloadを作り、1行ずつAPIへPOSTする。"""
        payloads = self.build_payloads(layout, rows)

        if self.settings.dry_run:
            print(f"[DRY-RUN] {layout['id']}: {len(payloads)} rows would be sent")
            return

        if not self.settings.api_endpoint:
            raise ValueError("API_ENDPOINT が未設定です。DRY_RUN=true か API_ENDPOINT を設定してください。")

        endpoint = self._resolve_endpoint(layout)
        for payload in payloads:
            self._post_json(endpoint, payload)

    def build_payloads(self, layout: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """履歴保存とAPI送信で共通利用するpayload一覧を作る。"""
        payloads = [self._build_payload(layout, row) for row in rows]
        return [payload for payload in payloads if payload]

    def _resolve_endpoint(self, layout: dict[str, Any]) -> str:
        """API_ENDPOINT とレイアウト別 path を結合して送信先URLを作る。"""
        base = self.settings.api_endpoint.rstrip("/")
        path = layout.get("api", {}).get("path", "").strip("/")
        return f"{base}/{path}" if path else base

    def _build_payload(self, layout: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        """内部キーのデータを、APIへ送る項目名に変換する。"""
        payload: dict[str, Any] = {}
        for field in layout["fields"]:
            if field.get("send", True):
                payload[field.get("target", field["name"])] = row.get(field["name"])
        return payload

    def _post_json(self, endpoint: str, payload: dict[str, Any]) -> None:
        """標準ライブラリだけでJSONをPOSTする。HTTPエラー時は例外にする。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.settings.api_token:
            headers["Authorization"] = f"Bearer {self.settings.api_token}"

        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status >= 400:
                    raise RuntimeError(f"API送信に失敗しました: status={response.status}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"API送信に失敗しました: status={exc.code}, body={detail}") from exc


class FileProcessor:
    """1ファイル分の検知後処理をまとめて実行する。"""

    def __init__(self, settings: AppSettings):
        """判定、解析、検証、送信に必要な部品を準備する。"""
        self.settings = settings
        self.catalog = LayoutCatalog(settings.config_path)
        self.parser = CsvLayoutParser()
        self.validator = RowValidator()
        self.api = ApiClient(settings)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.history_path = settings.history_dir / f"{timestamp}_api_payloads.json"

    def process_waiting_file(self, file_path: Path) -> None:
        """waitingに届いたファイルをactiveへ移し、解析から完了/エラー移動まで行う。"""
        if not file_path.is_file() or file_path.name.startswith("."):
            return

        wait_until_file_is_stable(file_path, self.settings.file_stable_seconds)
        active_path = move_unique(file_path, self.settings.active_dir / file_path.name)
        print(f"[DETECTED] ファイルが届きました: {active_path.name}")

        try:
            layout = self.catalog.detect(active_path)
            if layout is None:
                raise ValueError("対応するレイアウトを判定できませんでした。ファイル名またはヘッダーを確認してください。")

            rows = self.parser.parse(active_path, layout)
            valid_rows, validation_errors = self.validator.validate(rows, layout)
            if validation_errors:
                raise ValueError("バリデーションエラー: " + " / ".join(validation_errors[:20]))

            self.api.send(layout, valid_rows)
            self.save_history(active_path.name, layout, valid_rows)
            normalize_csv_for_excel(active_path, layout)
            done_path = move_unique(active_path, self.settings.done_dir / active_path.name)
            print(f"[DONE] {layout['name']}: {len(valid_rows)} rows -> {done_path.name}")
        except Exception as exc:
            error_path = move_unique(active_path, self.settings.error_dir / active_path.name)
            write_error_file(error_path, exc)
            print(f"[ERROR] {error_path.name}: {exc}")

    def save_history(self, file_name: str, layout: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        """送信対象になったデータをJSON Lines形式で履歴保存する。"""
        if not self.settings.save_history_jsonl:
            return

        payloads = self.api.build_payloads(layout, rows)
        if self.history_path.exists():
            history = json.loads(self.history_path.read_text(encoding="utf-8-sig"))
        else:
            history = {
                "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "dryRun": self.settings.dry_run,
                "files": [],
            }

        history["files"].append(
            {
                "sourceFile": file_name,
                "layoutId": layout["id"],
                "layoutName": layout["name"],
                "apiPath": layout.get("api", {}).get("path", ""),
                "count": len(payloads),
                "payloads": payloads,
            }
        )

        self.history_path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8-sig",
        )


def move_unique(source: Path, destination: Path) -> Path:
    """移動先に同名ファイルがある場合はタイムスタンプを付けて安全に移動する。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.move(str(source), str(destination))
        return destination

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_destination = destination.with_name(f"{destination.stem}_{timestamp}{destination.suffix}")
    shutil.move(str(source), str(unique_destination))
    return unique_destination


def write_error_file(error_path: Path, exc: Exception) -> None:
    """エラーになったファイルの横に、原因を書いたテキストファイルを保存する。"""
    detail_path = error_path.with_suffix(error_path.suffix + ".error.txt")
    detail_path.write_text(str(exc), encoding="utf-8")


def normalize_csv_for_excel(file_path: Path, layout: dict[str, Any]) -> None:
    """処理済みCSVをExcelで開きやすいUTF-8 BOM付きに変換する。"""
    if layout.get("type") != "csv":
        return

    for encoding in layout.get("encodings", ["utf-8-sig", "cp932"]):
        try:
            with file_path.open("r", encoding=encoding, newline="") as source:
                rows = list(csv.reader(source))
            break
        except UnicodeDecodeError:
            continue
    else:
        return

    with file_path.open("w", encoding="utf-8-sig", newline="") as destination:
        writer = csv.writer(destination, quoting=csv.QUOTE_MINIMAL)
        writer.writerows(rows)


def wait_until_file_is_stable(file_path: Path, stable_seconds: float) -> None:
    """コピー途中のファイルを読まないよう、ファイルサイズが安定するまで待つ。"""
    previous_size = -1
    stable_started_at = time.monotonic()
    while True:
        current_size = file_path.stat().st_size
        if current_size != previous_size:
            previous_size = current_size
            stable_started_at = time.monotonic()
        if time.monotonic() - stable_started_at >= stable_seconds:
            return
        time.sleep(0.2)


def load_dotenv_file(path: Path) -> None:
    """python-dotenvなしで、単純なKEY=VALUE形式の.envを読み込む。"""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def recover_active_files(settings: AppSettings) -> None:
    """前回の異常終了でactiveに残ったファイルをwaitingへ戻して再処理できるようにする。"""
    for file_path in settings.active_dir.iterdir():
        if file_path.is_file() and not file_path.name.startswith("."):
            move_unique(file_path, settings.waiting_dir / file_path.name)


def cleanup_output_files(settings: AppSettings) -> None:
    """done/error/historyの古いファイルを削除し、増え続けないようにする。"""
    cleanup_directory(settings.done_dir, settings.done_retention_days, settings.max_done_files)
    cleanup_directory(settings.error_dir, settings.error_retention_days, settings.max_error_files)
    cleanup_directory(settings.history_dir, settings.history_retention_days, settings.max_history_files)


def cleanup_directory(directory: Path, retention_days: int, max_files: int) -> None:
    """指定フォルダ内のファイルを、保存日数と最大件数で整理する。"""
    if not directory.exists():
        return

    files = [file_path for file_path in directory.iterdir() if file_path.is_file()]
    now = time.time()

    if retention_days >= 0:
        max_age_seconds = retention_days * 24 * 60 * 60
        for file_path in files:
            if now - file_path.stat().st_mtime > max_age_seconds:
                file_path.unlink(missing_ok=True)

    if max_files <= 0:
        return

    remaining_files = [file_path for file_path in directory.iterdir() if file_path.is_file()]
    remaining_files.sort(key=lambda file_path: file_path.stat().st_mtime, reverse=True)
    for file_path in remaining_files[max_files:]:
        file_path.unlink(missing_ok=True)


def run_once(settings: AppSettings) -> None:
    """waitingフォルダにある既存ファイルを1回だけ処理する。"""
    settings.ensure_dirs()
    cleanup_output_files(settings)
    recover_active_files(settings)
    processor = FileProcessor(settings)
    for file_path in sorted(settings.waiting_dir.iterdir()):
        processor.process_waiting_file(file_path)


def run_watch(settings: AppSettings) -> None:
    """waitingフォルダを監視し、新規/更新ファイルを見つけたら処理する。"""
    settings.ensure_dirs()
    cleanup_output_files(settings)
    recover_active_files(settings)
    processor = FileProcessor(settings)
    run_once(settings)
    print(f"[WATCH] 監視を開始しました: {settings.waiting_dir}")
    if watch is None:
        run_polling_watch(settings, processor)
        return

    for changes in watch(settings.waiting_dir):
        for change, changed_path in changes:
            if change in (Change.added, Change.modified):
                processor.process_waiting_file(Path(changed_path))


def run_polling_watch(settings: AppSettings, processor: FileProcessor) -> None:
    """watchfilesが使えない環境向けに、1秒ごとにフォルダを見に行く監視処理。"""
    seen: dict[Path, float] = {}
    print("[WATCH] watchfiles が無いため、ポーリング監視で動作します")
    while True:
        for file_path in sorted(settings.waiting_dir.iterdir()):
            if file_path.is_file() and not file_path.name.startswith("."):
                modified_at = file_path.stat().st_mtime
                if seen.get(file_path) != modified_at:
                    seen[file_path] = modified_at
                    processor.process_waiting_file(file_path)
        time.sleep(1.0)


def build_arg_parser() -> argparse.ArgumentParser:
    """コマンドライン引数の定義を作る。"""
    parser = argparse.ArgumentParser(description="ZKONG向けファイル監視・変換・API送信")
    parser.add_argument("--watch", action="store_true", help="waitingフォルダを監視し続けます")
    parser.add_argument("--once", action="store_true", help="waitingフォルダ内のファイルを1回だけ処理します")
    parser.add_argument("--dry-run", action="store_true", help="APIへ送信せず、解析と検証だけ実行します")
    return parser


def main() -> None:
    """コマンドライン引数を読み、単発処理または監視処理を開始する。"""
    args = build_arg_parser().parse_args()
    settings = AppSettings.from_env(dry_run=args.dry_run)
    if args.watch:
        run_watch(settings)
    else:
        run_once(settings)


if __name__ == "__main__":
    main()
