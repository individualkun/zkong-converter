import os
import sys
import json
import csv
import requests
from datetime import datetime
from dotenv import load_dotenv
from watchfiles import watch

# 1. 環境設定の読み込み
load_dotenv()

API_ENDPOINT = os.getenv("API_ENDPOINT", "")
WAITING_DIR = os.getenv("WAITING_DIR", "data/waiting")
ACTIVE_DIR = os.getenv("ACTIVE_DIR", "data/active")
HISTORY_DIR = os.getenv("HISTORY_DIR", "data/history")
# CSVを保存するかどうかのフラグ（大文字小文字を区別せず true なら True にする）
SAVE_HISTORY_CSV = os.getenv("SAVE_HISTORY_CSV", "false").lower() == "true"

FORMAT_DEF_PATH = "format/formats.json"

def setup_folders():
    """必要なフォルダがなければ自動作成する"""
    for d in [WAITING_DIR, ACTIVE_DIR, HISTORY_DIR]:
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)

def load_formats():
    """formats.json から定義を読み込む"""
    if not os.path.exists(FORMAT_DEF_PATH):
        print(f"Error: 定義ファイル {FORMAT_DEF_PATH} が見つかりません。")
        return None
    with open(FORMAT_DEF_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def identify_format(file_name, formats):
    """ファイル名から対応するフォーマット定義を特定する"""
    upper_name = file_name.upper()
    for key, config in formats.items():
        # match_pattern を取得して大文字に変換し、ファイル名と比較する
        pattern = config.get("match_pattern", "").upper()
        if pattern and pattern in upper_name:
            return config["columns"] # columns（項目リスト）を返す
    return None

def parse_fixed_length(file_path, format_definition):
    """固定長ファイルを解析して全行をリスト化する"""
    parsed_rows = []
    with open(file_path, "r", encoding="cp932", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            
            row_data = {}
            # formats.json に書かれているすべての項目を一旦切り出す
            for item in format_definition:
                start = item["start"] - 1  # 1始まりを0始まりに変換
                end = start + item["length"]
                val = line[start:end].strip()
                
                # 型変換
                if item.get("type") == "int":
                    val = int(val) if val.isdigit() else 0
                
                row_data[item["name"]] = val
            
            parsed_rows = parsed_rows + [row_data]
    return parsed_rows

def export_to_csv(file_name, parsed_rows, format_definition):
    """enabled: true の項目だけを抽出してCSVに出力する"""
    # enabled: true の項目の「手元の名前」をヘッダーにする
    csv_headers = [item["name"] for item in format_definition if item.get("enabled") is True]
    
    if not csv_headers:
        print("--- [CSVスキップ] 有効な(enabled:true)項目がないためCSVは作成しません ---")
        return

    # history_ファイル名_日時.csv という名前を作る
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = os.path.splitext(file_name)[0]
    csv_file_name = f"history_{base_name}_{timestamp}.csv"
    csv_path = os.path.join(HISTORY_DIR, csv_file_name)

    # Excelでの文字化けを防ぐため utf-8-sig で出力
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=csv_headers)
        writer.writeheader()
        
        for row in parsed_rows:
            # enabled: true のデータだけを間引く
            filtered_row = {k: v for k, v in row.items() if k in csv_headers}
            writer.writerow(filtered_row)
            
    print(f"--- [CSV保存完了] {csv_path} に履歴を保存しました ---")

def send_to_api(parsed_rows, format_definition):
    """enabled: true の項目だけを厳選し、名前を alias に変換してAPIに送信する"""
    # 最初にURLが正しいか（httpから始まっているか）をチェック！
    if not API_ENDPOINT or not API_ENDPOINT.startswith("http"):
        print("[APIスキップ] API_ENDPOINT が未設定、または無効なURLのため送信をスキップしました。")
        return  # ここで関数を終了するので、下のループには入らない！

    success_count = 0
    for row in parsed_rows:
        api_payload = {}
        for item in format_definition:
            if item.get("enabled") is True:
                send_name = item.get("alias") or item["name"]
                api_payload[send_name] = row[item["name"]]
        
        if not api_payload:
            continue

        try:
            response = requests.post(API_ENDPOINT, json=api_payload, timeout=10)
            if response.status_code == 200:
                success_count += 1
            else:
                print(f"[API警告] 送信失敗 Status: {response.status_code} - Data: {api_payload}")
        except Exception as e:
            print(f"[APIエラー] 通信に失敗しました: {e}")
            
    print(f"--- [API送信完了] {success_count} / {len(parsed_rows)} 行の送信に成功しました ---")

def process_file(file_path):
    """ファイル処理のメインフロー"""
    file_name = os.path.basename(file_path)
    print(f"\n--- [処理開始] {file_name} ---")
    
    formats = load_formats()
    if not formats:
        return
        
    # 特定したフォーマット定義から columns（列定義）を引っこ抜く
    format_config = identify_format(file_name, formats)
    if not format_config:
        print(f"[スキップ] ファイル名「{file_name}」にマッチする定義が formats.json にありません。")
        return
    
    format_definition = format_config
        
    # 1. アクティブ（処理中）フォルダへ移動
    active_path = os.path.join(ACTIVE_DIR, file_name)
    try:
        os.rename(file_path, active_path)
    except Exception as e:
        print(f"ファイルの移動に失敗しました: {e}")
        return

    # 2. 固定長ファイルの解析
    parsed_rows = parse_fixed_length(active_path, format_definition)
    print(f"--- [解析完了] {len(parsed_rows)} 行のデータを読み込みました ---")

    if not parsed_rows:
        print("--- [終了] データが空のため処理を終了します ---")
        if os.path.exists(active_path):
            os.remove(active_path)
        return

    # 3. 必要に応じてCSVに保存（.env の SAVE_HISTORY_CSV が true の場合のみ）
    if SAVE_HISTORY_CSV:
        export_to_csv(file_name, parsed_rows, format_definition)

    # 4. APIへのデータ送信（enabled: true のみ間引き送信）
    send_to_api(parsed_rows, format_definition)

    # 5. オンプレ仕様：処理が終わったら生ファイルは即座に削除
    if os.path.exists(active_path):
        os.remove(active_path)
    print(f"--- [完了] {file_name} の全処理が成功し、生ファイルを削除しました ---")

def start_watch_mode():
    """常駐監視モードの起動"""
    setup_folders()
    
    # 【自動リカバリー機能】active にファイルが残っていたら waiting に戻す
    for f in os.listdir(ACTIVE_DIR):
        active_file = os.path.join(ACTIVE_DIR, f)
        if os.path.isfile(active_file) and not f.startswith("."):
            print(f"[リカバリー] 異常終了により残っていたファイルを復旧します: {f}")
            os.rename(active_file, os.path.join(WAITING_DIR, f))

    print(f"--- [監視モード起動] {WAITING_DIR} ---")
    if SAVE_HISTORY_CSV:
        print(f"[CSV保存モード]: ON (保存先: {HISTORY_DIR})")
    else:
        print(f"[CSV保存モード]: OFF")
    
    # 起動時にすでに waiting フォルダにあるファイルを処理
    for f in os.listdir(WAITING_DIR):
        full_path = os.path.join(WAITING_DIR, f)
        if os.path.isfile(full_path) and not f.startswith("."):
            process_file(full_path)

    # 新しく waiting フォルダに入ってくるファイルをリアルタイム監視
    for changes in watch(WAITING_DIR):
        for change_type, file_path in changes:
            # 🌟 新規作成(1) または 上書き(2) どちらでも反応するように変更！
            if change_type.value in (1, 2):
                if os.path.isfile(file_path) and not os.path.basename(file_path).startswith("."):
                    process_file(file_path)                    

if __name__ == "__main__":
    # コマンド引数に --watch があれば監視モードで起動
    if len(sys.argv) > 1 and sys.argv[1] == "--watch":
        start_watch_mode()
    else:
        # 引数なしの場合は単発処理（一応残してあります）
        setup_folders()
        files = [os.path.join(WAITING_DIR, f) for f in os.listdir(WAITING_DIR) if os.path.isfile(os.path.join(WAITING_DIR, f)) and not f.startswith(".")]
        if not files:
            print(f"{WAITING_DIR} に処理対象のファイルがありません。")
        for f in files:
            process_file(f)