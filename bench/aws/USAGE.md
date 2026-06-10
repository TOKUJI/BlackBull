# bench/aws 用 USAGE

## 目的

`bench/aws/` は、BlackBull のベンチマークを AWS EC2 上で実行するためのハーネスです。

主な役割は次のとおりです。

- `up.sh` : EC2 インスタンスを起動して、SSH で接続できる状態にする
- `install.sh` : リポジトリとベンチツールを EC2 に配布し、依存関係を整える
- `run.sh` : `bench/peers/compare_servers.sh` を遠隔実行し、結果をローカルへ回収する
- `down.sh` : 作成した EC2 / SG / キーペア / 状態ファイルを削除する
- `profile.sh` / `profile_lanes.sh` : プロファイルやレーン別の計測を行う
- `httparena_compare.sh` : `bench/httparena/` を HttpArena にベンダリングして、AWS 上で公式 validate / benchmark を走らせる

---

## 事前準備

ローカルマシンに次を用意してください。

- `aws` CLI
- `ssh`
- `rsync`
- `jq`
- `curl`

AWS 認証は次のいずれかで済ませます。

```bash
aws login
# または
aws sso login --profile <name>
# または
aws configure
```

`bench/aws/config.sh` が `aws sts get-caller-identity` を実行するため、実行前に認証済みであることが必要です。

---

## 1. まずは基本の 4 ステップで実行する

### 1-1. EC2 を起動する

```bash
bash bench/aws/up.sh
```

- 既定では `TOPO=single` です。
- `TOPO=split` にすると、サーバーと負荷生成器を別インスタンスに分けます。

インスタンスサイズは環境変数で指定できます。

```bash
INSTANCE_TYPE=c7i.2xlarge \
LOADGEN_INSTANCE_TYPE=c7i.4xlarge \
bash bench/aws/up.sh
```

- `INSTANCE_TYPE` : 単一ホスト時のサーバー／`TOPO=single` のインスタンスサイズ
- `LOADGEN_INSTANCE_TYPE` : `TOPO=split` の負荷生成器サイズ

`bench/aws/config.sh` の既定値は `INSTANCE_TYPE=c7i.xlarge`、`LOADGEN_INSTANCE_TYPE=c7i.2xlarge` です。

### 1-2. リモート環境をセットアップする

```bash
bash bench/aws/install.sh
```

この段階で次を実施します。

- リポジトリ全体を EC2 へ rsync
- Python / ベンチ依存関係をインストール
- `bench/install.sh` で h2load, wrk, wrk2, oha, k6 などを入れる
- TLS 証明書をインストール
- 単体テストの smoke test を実施

### 1-3. ベンチマークを実行する

```bash
bash bench/aws/run.sh
```

- `compare_servers.sh` を EC2 上で実行します。
- 結果は実行時に生成される UTC タイムスタンプ付きディレクトリ（例: `bench/results/aws/20260605-123456Z/`）に回収されます。
- 既定では比較対象の行列をそのまま走らせます。
- さらに `run.sh` 自体は `bench/results/aws/<生成されたタイムスタンプ>/run.log` に実行ログを保存します。

### 1-4. 後片付けする

```bash
bash bench/aws/down.sh
```

- EC2 / SG / キーペア / 状態ファイルを削除します。
- 失敗したリソースの残りを確認してくれます。

---

## 2. split トポロジで実行する

負荷生成器とサーバーを分離して測定したい場合は `TOPO=split` を使います。

```bash
TOPO=split bash bench/aws/up.sh
TOPO=split bash bench/aws/install.sh
TOPO=split bash bench/aws/run.sh
TOPO=split bash bench/aws/down.sh
```

特徴:

- サーバーは `c7i.xlarge`
- 負荷生成器は `c7i.2xlarge`
- VPC 内のプライベート通信で実行
- `compare_servers.sh` は負荷生成器側で起動し、サーバーを SSH 経由で制御します

`TOPO=split` は、単一ホスト計測よりも実測のノイズを減らしやすいです。

---

## 3. 実行条件を少しだけ変える

`run.sh` は環境変数で比較対象を調整できます。

```bash
LANES="A B-wrk" \
STACKS="blackbull granian" \
DURATION=30 \
RUNS=3 \
bash bench/aws/run.sh
```

主な変数:

- `RUNS` : h2load の繰り返し回数
- `LANES` : 走らせるロードレーン
- `STACKS` : 比較対象フレームワーク
- `DURATION` : wrk / oha の測定時間（秒）

---

## 4. プロファイル計測を追加する

### 4-1. py-spy の flame graph を取得する

```bash
bash bench/aws/profile.sh
```

- EC2 上で py-spy を回し、SVG / JSON を回収します。
- 出力先は `bench/results/aws/<生成されたタイムスタンプ>/profile/` です。

### 4-2. Lane 別の py-spy / wrk を取得する

```bash
TOPO=split bash bench/aws/profile_lanes.sh
```

- `B1 / B2 / B3` などのレーン毎に、py-spy と wrk を回し、speedscope 形式の JSON を回収します。
- `profile.json` を https://www.speedscope.app にドラッグすると flame graph が見えます。

---

## 5. `bench/httparena/` と連携して AWS 上で HttpArena を回す

この AWS ハーネスは、前段の `bench/httparena/` と組み合わせて、EC2 上で HttpArena の公式検証・ベンチマークを走らせることができます。

### 5-1. まず `bench/httparena/` 側を更新する

`bench/httparena/app.py` / `launcher.py` / `meta.json` を編集します。

この変更は、後続の EC2 実行でそのまま HttpArena の `blackbull` フレームワークとして使われます。

### 5-2. AWS 上で HttpArena 連携を実行する

```bash
bash bench/aws/httparena_compare.sh
```

このスクリプトは、結果をローカルの `bench/results/httparena/<SPRINT_TAG>-<生成されたタイムスタンプ>/` に回収します。`SPRINT_TAG` を変えると保存先のプレフィックスを変えられます。ここでの `<生成されたタイムスタンプ>` はスクリプト実行時に `date -u +%Y%m%d-%H%M%SZ` で作る値であり、別途 `TS` 環境変数を渡す必要はありません。

`bench/httparena/` の「開発用 wheel ベース Docker」手順と組み合わせて使う場合は、まずローカルで wheel を作り、AWS 側の framework 生成手順に差し替えます。

```bash
# 1) ローカルで開発用 wheel を作る
python -m build --wheel

# 2) 生成された wheel を EC2 へ転送し、HttpArena の blackbull フレームワークを
#    bench/httparena/Dockerfile.dev と同じ考え方で動かす
#    （必要なら rsync で wheel を置き、remote 側の Dockerfile を dev 版に差し替える）
```

具体的には、`bench/aws/httparena_compare.sh` が作る `HttpArena/frameworks/blackbull/Dockerfile` を、`bench/httparena/Dockerfile.dev` の流れに合わせて手動で差し替えれば、未公開のローカル wheel を AWS 上でも検証できます。

このスクリプトは内部で次を行います。

1. EC2 を起動する
2. Docker / gcannon / wrk / h2load をインストールする
3. `MDA2AV/HttpArena` を EC2 に clone する
4. `bench/httparena/` を HttpArena の `frameworks/blackbull/` 配下へベンダリングする
5. `meta.json` の `enabled=true` に切り替える
6. `scripts/validate.sh blackbull` を実行する
7. `scripts/benchmark.sh blackbull <profile>` を実行する
8. ログと結果をローカルの `bench/results/httparena/<SPRINT_TAG>-<生成されたタイムスタンプ>/` へ回収する

### 5-3. 代表的な環境変数

```bash
PROFILES="baseline json json-tls static" \
FRAMEWORKS="blackbull fastapi" \
BLACKBULL_VERSION=0.28.0 \
BLACKBULL_WORKERS=4 \
FASTAPI_WORKERS=4 \
SPRINT_TAG=sprint29 \
SKIP_VALIDATE=0 \
KEEP_INSTANCE=0 \
bash bench/aws/httparena_compare.sh
```

例として、BlackBull と FastAPI の両方を 4 worker で比較したいときは次のようにします。

```bash
ULIMIT_N=65536 \
BLACKBULL_WORKERS=4 FASTAPI_WORKERS=4 \
FRAMEWORKS="blackbull fastapi" \
bash bench/aws/httparena_compare.sh
```

`ULIMIT_N` を付けると、HttpArena の validate / benchmark を開始する前に `ulimit -n` を適用してから `wrk` と Docker コンテナを起動します。必要に応じて 1024 などに下げることもできます。

`LOADGEN_CPU_MAX=24` あるいは `LOADGEN_CPU_RATIO=0.75` を付けると、HttpArena の `gcannon/wrk` 側の CPU 使用帯域を上限付きで固定できます。さらに `PROFILE_CPU_MAX=24` / `PROFILE_CPU_RATIO=0.75` を付けると、HttpArena プロファイル側の CPU 上限（`framework.sh` の cpuset キャップ）もその値に合わせて調整できます。例: c7i.8xlarge では `LOADGEN_CPU_RATIO=0.75` で 24 CPU 相当、`BLACKBULL_WORKERS=2` でサーバー側を 2 worker に絞った比較ができます。

- `BLACKBULL_WORKERS` : AWS 側の `bench/httparena` 用 BlackBull コンテナで使う worker 数
- `FASTAPI_WORKERS` : HttpArena の FastAPI フレームワーク側で使う worker 数
- `ULIMIT_N` : `wrk` / Docker 起動前に適用する FD 制限（例: 65536）
- `LOADGEN_CPU_MAX` : `gcannon/wrk` 側の最大 CPU 数（例: 24）
- `LOADGEN_CPU_RATIO` : `gcannon/wrk` 側の最大 CPU 割合（例: 0.75 = 75%）
- `PROFILE_CPU_MAX` : HttpArena プロファイル側の CPU 上限（例: 24）
- `PROFILE_CPU_RATIO` : HttpArena プロファイル側の CPU 割合（例: 0.75 = 75%）

- `PROFILES` : 走らせる HttpArena プロファイル
- `FRAMEWORKS` : 比較対象フレームワーク
- `BLACKBULL_VERSION` : EC2 コンテナ内にインストールする BlackBull のバージョン
- `SKIP_VALIDATE=1` : validate を飛ばして benchmark のみ実施
- `KEEP_INSTANCE=1` : 失敗解析のため EC2 を残す

### 5-4. この連携の意味

`bench/httparena/` でローカル確認した実装を、そのまま AWS 上で正式な HttpArena の検証手順に載せたいときに使います。

つまり、

- ローカルで `bench/httparena/` の動作確認
- AWS 上で `httparena_compare.sh` による公式相当のベンチ実行

の二段構えで、実機の数値を確認できます。

---

## 6. よく使う実行パターン

### A. 通常の比較ベンチ

```bash
bash bench/aws/up.sh
bash bench/aws/install.sh
bash bench/aws/run.sh
bash bench/aws/down.sh
```

### B. split トポロジでの本格計測

```bash
TOPO=split bash bench/aws/up.sh
TOPO=split bash bench/aws/install.sh
TOPO=split bash bench/aws/run.sh
TOPO=split bash bench/aws/down.sh
```

### C. `bench/httparena/` を AWS で正式検証する

```bash
bash bench/aws/httparena_compare.sh
```

### D. flame graph / レーン別分析

```bash
bash bench/aws/profile.sh
TOPO=split bash bench/aws/profile_lanes.sh
```

---

## 7. 注意点

- `bench/aws/` は EC2 を起動・停止するため、AWS 認証と課金の理解が必要です。
- `TOPO=split` は 2 台起動するので、単一ホストよりコストが高くなります。
- `httparena_compare.sh` は `bench/httparena/` を自動ベンダリングするため、HttpArena 側での確認に最適です。
- 実行後は必ず `down.sh` で片付けるのが基本です。

---

## すぐ始めるなら

最短の流れは次です。

```bash
aws login
bash bench/aws/up.sh
bash bench/aws/install.sh
bash bench/aws/run.sh
bash bench/aws/down.sh
```

`bench/httparena/` の変更を AWS で正式に試したい場合は、最後に次も追加します。

```bash
bash bench/aws/httparena_compare.sh
```
