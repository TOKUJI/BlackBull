# BlackBull / HttpArena 用 USAGE

## 目的

このディレクトリは、BlackBull を HttpArena のベンチマーク・ハーネスに載せるためのローカル用プレパレーションです。

- `app.py`: HttpArena 向けのエンドポイントを持つ BlackBull アプリ
- `launcher.py`: HTTP/HTTPS のプロセスを起動するラッパー
- `Dockerfile` / `Dockerfile.dev`: コンテナ実行用のビルド設定
- `meta.json`: HttpArena へのフレームワーク登録メタデータ

現時点では `meta.json` の `enabled: false` のため、**リーダーボード提出用ではなく、ローカル検証・将来の統合準備**として使います。

---

## どんな機能を提供するか

`app.py` は、BlackBull が現状サポートしている主要プロファイルに対応したエンドポイントを実装しています。

主なルート:

- `GET /pipeline` → `ok`
- `GET/POST /baseline11` → クエリ数値と POST ボディを足した結果を返す
- `GET /json/{count}` → JSON レスポンスを返す
- `POST /upload` → ボディ長を返す
- `GET /ws` → WebSocket echo

また、`launcher.py` は次のポートを起動します。

- `:8080` → HTTP/1.1（h2c prior-knowledge も含む）
- `:8081` → HTTPS / HTTP/1.1
- `:8443` → HTTPS / HTTP/2（ALPN）

---

## まず最初に必要な準備

ローカルで動かすときは、HttpArena が想定するデータと証明書を用意します。

```bash
mkdir -p bench/httparena/_local/data bench/httparena/_local/certs

curl -fL -o bench/httparena/_local/data/dataset.json \
  https://raw.githubusercontent.com/MDA2AV/HttpArena/master/data/dataset.json

cp tests/cert.pem bench/httparena/_local/certs/server.crt
cp tests/key.pem  bench/httparena/_local/certs/server.key
```

- `_local/` はローカル用の一時ディレクトリです。
- ここに置いたデータは、Docker 実行時に `/data` / `/certs` としてマウントされます。

---

## 1. コンテナでローカル smoke test を実行する

リポジトリのルートから実行します。

```bash
docker build -f bench/httparena/Dockerfile -t blackbull-httparena .

docker run --rm --network host \
  -v "$PWD/bench/httparena/_local/data:/data:ro" \
  -v "$PWD/bench/httparena/_local/certs:/certs:ro" \
  --name blackbull-httparena \
  blackbull-httparena
```

別ターミナルで確認します。

```bash
curl -s "http://localhost:8080/pipeline"
curl -s "http://localhost:8080/baseline11?a=1&b=2&c=3"
curl -s "http://localhost:8080/json/3?m=2.0" | python3 -m json.tool
curl -sk "https://localhost:8081/baseline11?a=10"
```

期待値:

- `/pipeline` → `ok`
- `/baseline11?a=1&b=2&c=3` → `6`
- `/json/3?m=2.0` → JSON レスポンス
- HTTPS 側でも応答する

### 実行結果の保存先

- `docker build` はローカルの Docker イメージとして保存されます（`blackbull-httparena` / `blackbull-httparena:dev`）。結果ファイルは作成されません。
- `docker run` の標準出力・標準エラーはそのままターミナルに出ます。ログを残したい場合は、次のように保存先を指定してください。

```bash
docker run --rm --network host \
  -v "$PWD/bench/httparena/_local/data:/data:ro" \
  -v "$PWD/bench/httparena/_local/certs:/certs:ro" \
  blackbull-httparena 2>&1 | tee bench/results/httparena/smoke-$(date -u +%Y%m%d-%H%M%SZ).log
```

- `validate.sh` / `benchmark.sh` も標準出力にログを出します。保存先を明示したいときは `tee` で任意のファイルにリダイレクトしてください。

---

## 2. 開発用の wheel ベース Docker で実行する

未公開の変更を含むローカル wheel を使いたい場合は `Dockerfile.dev` を使います。

```bash
docker build -f bench/httparena/Dockerfile.dev -t blackbull-httparena:dev bench/httparena/
```

このファイルは、PyPI ではなくローカルの `blackbull-*.whl` をインストールして動かします。

---

## 3. HttpArena 本体で validate / benchmark を走らせる

実際の HttpArena の検証スクリプトを使う場合は、HttpArena を別ディレクトリにクローンし、frameworks 配下にこのディレクトリをコピーします。

```bash
git clone https://github.com/MDA2AV/HttpArena.git ~/work/HttpArena

mkdir -p ~/work/HttpArena/frameworks/blackbull
cp -r bench/httparena/* ~/work/HttpArena/frameworks/blackbull/
```

その後、ベンダリング先の `meta.json` で `enabled: true` に変更し、HttpArena 側で実行します。

```bash
cd ~/work/HttpArena
./scripts/validate.sh blackbull
./scripts/benchmark.sh blackbull baseline
```

- `validate.sh` は 18 点の整合性チェック
- `benchmark.sh` は指定プロファイルのベンチマーク実行

保存先を明示したい場合は、次のように `tee` でログファイルを指定できます。

```bash
cd ~/work/HttpArena
./scripts/validate.sh blackbull 2>&1 | tee ~/work/HttpArena/results/validate-blackbull.log
./scripts/benchmark.sh blackbull baseline 2>&1 | tee ~/work/HttpArena/results/benchmark-blackbull-baseline.log
```

---

## 自動化されたローカル検証（WSL2 validate → EC2 ready-check）

correctness 検証はハードウェア非依存のため、フル 20 プロファイルの
`validate.sh` をローカルで実行し、EC2 では最小 ready-check のみにするのが
標準フローです（EC2 時間を 1 回あたり約 15 分節約）。

```bash
# 1. 任意の git ref から wheel をビルドし sha256 を記録
bash bench/httparena/build_wheel.sh HEAD

# 2. その wheel に対して HttpArena の validate.sh を全プロファイル実行
bash bench/httparena/validate_local.sh HEAD
#    → bench/results/httparena-local/<UTC>/verdict.txt（PASS / FAIL / …）
```

`validate_local.sh` は初回に `MDA2AV/HttpArena` を `~/HttpArena` へ clone し、
`patch_httparena.py` / `patch_cpuset.sh` を適用、フレームワーク staging・
イメージ pre-build（validate.sh の 300 秒 watchdog 対策）・
`VALIDATE_WALL` バウンド付きで `validate.sh` を実行します。

EC2 側は **同じ wheel ファイル** を `BB_WHEEL_PATH` で渡し、
`SKIP_VALIDATE=1` にすると `run_httparena.sh` がフル validate の代わりに
`ready_check.sh`（コンテナ起動 / ポート / WS エコー / TLS / h2c / gRPC unary
の最小スモーク）を実行します。同一性の規律（検証と計測で同一 wheel）を
守ってください。

---

## 注意点

- このディレクトリは「HttpArena 連携の準備段階」にあるため、現在はローカル検証向けです。
- ベンチマーク時は `BB_ACCESS_LOG=0` にして、他フレームワークとの比較条件を揃えています。
- HTTP/3 などのプロファイルは未対応です（`static-h3`, `*-h3`, `grpc`, DB 系など）。

---

## すぐ試すなら

最短経路は、次の 3 コマンドです。

```bash
mkdir -p bench/httparena/_local/data bench/httparena/_local/certs
curl -fL -o bench/httparena/_local/data/dataset.json https://raw.githubusercontent.com/MDA2AV/HttpArena/master/data/dataset.json
cp tests/cert.pem bench/httparena/_local/certs/server.crt && cp tests/key.pem bench/httparena/_local/certs/server.key
```

その後、上記の `docker build` / `docker run` を実行してください。
