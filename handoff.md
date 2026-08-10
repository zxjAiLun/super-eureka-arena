# ChessArena Handoff

> 状态快照：2026-08-08
> 仓库：`E:\AUbuntuProject\project\chessenginearena`（super-eureka-arena）
> 分支：`main`
> 当前提交：`fa640c4e0575b46ee8f7fe6b13e54b3e6876b360`
> 远端：`origin/main`（https://github.com/zxjAiLun/super-eureka-arena）与本地 `HEAD` 一致
> Standalone CI：GREEN（run `31256574201`：arena pytest + replay-app build 均 success）

## 一句话结论

ChessArena 已完成从 ChessEngine 源码仓库的独立拆分（REPO SPLIT CLOSED），作为独立控制平面仓库运行：pytest 199 passed / 6 skipped、Playwright E2E、frontend build freshness 全绿。生产尚未部署；下一步是 production migration + capability backfill + 官方 8moves_v3 注册 + PGN smoke，之后进入 P4.3 Live Spectating。

## 设计决策（frozen）

- EngineVersion identity model（EngineBuild -> EnginePreset -> EngineVersion(=Elo participant) -> EngineChannel + RatingPool/Rating；--profile 仅限同源码树实验隔离）：见 [docs/design/engine-version-identity.md](docs/design/engine-version-identity.md)。Schema 实现推迟到 S4.3E promotion 之后。

## 仓库边界（拆分的核心契约）

```text
super-eureka（Engine 仓库，zxjAiLun/super-eureka）
    │  只发布 immutable EngineArtifact（binary + manifest.json）
    ▼
super-eureka-arena（本仓库）
    EngineBuild（SHA 重算 + 真实 UCI probe → capability schema）
        ▼
    EnginePreset（由 probed uci_options_schema 动态配置）
        ▼
    Tournament（frozen snapshot 驱动）
        ▼
    CuteChess
```

- Arena **不 import Engine 源码**；Engine repo **不跑 Python/Playwright**。
- 引擎 artifact 发布合同归 Engine repo 自测；GitHub SSH deploy 路径已废弃，**不恢复**。
- 官方 opening 书由本仓库 `opening-books/catalog.json` 自持，运行时绝不读 `../super-eureka/books`。

## 当前生产行为（代码已就绪，尚未部署）

### Foundation Repair（P4.F1）已全部完成并入历史

```text
Phase A   admin/runtime repair：danger button、pair 内 game 实时进度（RuntimePairStatus，
          /admin/tournaments/<id>/pairs 每 2s HTMX 刷新，从 stdout.log 推导，不写 authoritative Game）
Phase B   generic UCI capabilities：5 类型 parser（check/spin/combo/button/string + vars + 空格）、
          EngineBuild.uci_options_schema（migration 0006）、capability 冻结进 tournament snapshot、
          Hash/Threads/Ponder/OwnBook/UCI_Chess960 按 capability 单独发 + fail closed、
          动态 preset 编辑器（/admin/presets/new）、backfill 脚本 + deploy gate
Phase C   official opening books：OpeningSet format(pgn|epd)/source（migration 0007）、
          opening_plies（PGN replay N ply → FEN）、opening_seed 确定性抽样、
          snapshot 冻结 {sha256, format, plies, seed, indices}、磁盘文件 SHA fail-closed
Repair 1  verifier 与 scheduler 统一 resolver、catalog→registration adapter、
          实际文件 re-hash、PGN plies 默认合同（book default / 422）
```

### Deploy gate（部署时生效）

```text
worker refuse-to-start / health degraded，当存在 enabled build 且 uci_options_schema IS NULL
（health 暴露 uci_capability_gap；backfill 用 scripts/probe_build_capabilities.py）
```

### 公开 replay

`frontend/replay-app`（React + chess.js + react-chessboard）渲染 PGN replay：大棋盘、玩家卡、move list、导航、TC badges、PGN 下载、Lichess import。P4.UI-1 polish 已完成（黑上白下、3+2 友好 label、active move auto-scroll、Import into Lichess）。

## 目录结构

```text
chessarena/          FastAPI 应用（api/ services/ models.py schemas.py worker.py）
frontend/replay-app/ React replay（npm run build → chessarena/static/replay-app）
alembic/             migrations 0001..0007
deploy/              systemd unit、arena-deploy.sh、nginx 配置
scripts/             install_external_build.py、register_openings.py、
                     register_stockfish_presets.py、probe_build_capabilities.py
tests/               pytest 全套 + Playwright E2E + fixtures（fake cutechess/uci engine）
opening-books/       catalog.json（官方 Stockfish books）、prepare_books.py、cache(gitignored)
.github/workflows/   ci.yml（pytest + Playwright + replay-app build freshness）
```

## 测试

```text
本地 pytest:   199 passed / 6 skipped
Playwright:    test_browser_demo / test_browser_replay / test_browser_preset_editor
CI:            arena pytest（含 Playwright chromium）+ replay-app build freshness
无 Rust toolchain / 无真实引擎 binary 需求（fake cutechess + dummy engine）
```

## 尚未执行（生产部署清单）

```text
1. alembic upgrade 0006 → 0007
2. capability backfill：对每个 enabled build 跑 scripts/probe_build_capabilities.py <build-id>
   并确认 health uci_capability_gap == 0（否则 worker refuse-to-start）
3. 官方书注册：python opening-books/prepare_books.py 下载 →
   scripts/register_openings.py <cache/8moves_v3.pgn> --catalog opening-books/catalog.json \
     --book-id stockfish-8moves-v3
4. 真实 2-pair PGN-book smoke：scheduler → cutechess → verifier PASS →
   admin live progress → public replay
5. 之后：P4.3 Live Spectating（SSE → fen → react-chessboard，复用 RuntimePairStatus）
```

## 已记录的 P2（不阻塞）

```text
- ALLOWED_STRING_VALUES 留空（string option 的 Web 编辑策略按需扩展）
- 无其他已知 P0/P1
```

## 协作说明

- Arena 开发一律在 `E:\AUbuntuProject\project\chessenginearena` 进行；不要往 Engine 仓库塞 Arena 修复。
- Engine 历史归档 tag `arena-split-20260808`（super-eureka）保存拆分前 Arena 完整历史。
- 每阶段独立 commit、独立测试，禁止 mega-commit；不授权时不要部署生产。
