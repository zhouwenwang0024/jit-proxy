# python/sdk/jit_proxy/jitter/jitter_pair_arb.py
# -*- coding: utf-8 -*-
#
# 作用：
#   - 以 jitter_sniper 的“只在命中时出手”为风格，做你的“配对原子套利”（TT/MT/TM 三类两腿组合）
#   - 严格不盈利不广播：链下先筛选 + simulate，通过才 send；链上再由 arb_perp_plan 做护栏
#
# 依赖：
#   pip install anchorpy==0.16.0 solana==0.30.2
#
# 环境变量（示例）：
#   RPC_URL=...
#   WALLET_KEYPAIR_JSON='[.., .., ..]'        # u8 JSON 数组
#   JIT_PROXY_PROGRAM_ID=...                  # 你的 jit-proxy program id
#   DRIFT_PROGRAM_ID=...                      # drift program id
#   DRIFT_STATE=...
#   DRIFT_USER=...
#   DRIFT_USER_STATS=...
#   DRIFT_PERP_MARKET=...
#   DRIFT_ORACLE=...
#   DRIFT_QUOTE_SPOT_MARKET=...
#   MARKET_INDEX=0
#   MIN_EDGE_BPS=6
#   BASE_SZ=1000000000                        # 1e9 = 1.0 base
#   LOOP_INTERVAL_MS=200
#   IDL_PATH=target/idl/jit_proxy.json        # 用 anchor build 产出的 IDL
#
#   （演示喂价，可删）
#   MAKER_BID_PX=173100000 ; MAKER_BID_SZ=1000000000
#   MAKER_ASK_PX=173020000 ; MAKER_ASK_SZ=1000000000
#   # 如需要拍卖 taker 价：
#   # TAKER_BID_PX=173110000 ; TAKER_BID_SZ=1000000000
#   # TAKER_ASK_PX=173010000 ; TAKER_ASK_SZ=1000000000
#
import os
import json
import time
import asyncio
from dataclasses import dataclass
from typing import Optional, List, Tuple

from solana.publickey import PublicKey
from solana.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Processed
from solana.transaction import Transaction, AccountMeta
from solana.blockhash import Blockhash
from solana.rpc.types import TxOpts

from anchorpy import Provider, Wallet, Program, Context, Idl

# ===== 与链上 IDL 对齐的基础类型 =====
class PositionDirection:
    Long = 0
    Short = 1

class LegKind:
    Make = 0   # place_and_make（JIT maker）
    Take = 1   # place_and_take（IOC）

@dataclass
class PlanLeg:
    direction: int  # PositionDirection
    price: int      # PRICE_PRECISION (1e6) 的整数
    base_sz: int    # BASE_PRECISION  (1e9) 的整数
    kind: int       # LegKind

@dataclass
class Quote:
    price: int  # PRICE_PRECISION 整数
    size: int   # BASE_PRECISION  整数

# ===== 行情源（适配你现有订阅/拍卖流；像 sniper 一样只在命中时出手）=====
class QuoteSource:
    async def best_maker_bid(self) -> Optional[Quote]:
        return None
    async def best_maker_ask(self) -> Optional[Quote]:
        return None
    async def best_taker_bid(self) -> Optional[Quote]:
        return None
    async def best_taker_ask(self) -> Optional[Quote]:
        return None

class EnvQuoteSource(QuoteSource):
    """演示实现：从环境变量读取价量，方便先跑通流程；生产中替换为 DLOB/拍卖订阅"""
    async def best_maker_bid(self):
        px, sz = os.environ.get("MAKER_BID_PX"), os.environ.get("MAKER_BID_SZ")
        return Quote(int(px), int(sz)) if px and sz else None
    async def best_maker_ask(self):
        px, sz = os.environ.get("MAKER_ASK_PX"), os.environ.get("MAKER_ASK_SZ")
        return Quote(int(px), int(sz)) if px and sz else None
    async def best_taker_bid(self):
        px, sz = os.environ.get("TAKER_BID_PX"), os.environ.get("TAKER_BID_SZ")
        return Quote(int(px), int(sz)) if px and sz else None
    async def best_taker_ask(self):
        px, sz = os.environ.get("TAKER_ASK_PX"), os.environ.get("TAKER_ASK_SZ")
        return Quote(int(px), int(sz)) if px and sz else None

# ===== 策略工具 =====
def edge_bps(buy_px: int, sell_px: int) -> int:
    """(sell - buy) / buy * 1e4，sell>buy 才有正 edge"""
    if sell_px <= buy_px:
        return -1
    return int(((sell_px - buy_px) * 10_000) // buy_px)

def pick_best_plan(
    maker_bid: Optional[Quote],
    maker_ask: Optional[Quote],
    taker_bid: Optional[Quote],
    taker_ask: Optional[Quote],
    base_sz: int,
    min_edge_bps: int,
) -> Optional[Tuple[PlanLeg, PlanLeg, int, str]]:
    """
    枚举三种组合：TT / MT / TM，选 bps 最大且 >= min_edge_bps 的方案。
    返回：first_leg, second_leg, bps, tag
    """
    cands: List[Tuple[PlanLeg, PlanLeg, int, str]] = []

    # TT：makerBid ↔ makerAsk（两腿 TAKE/IOC）
    if maker_bid and maker_ask:
        buy_px, sell_px = maker_ask.price, maker_bid.price
        bps = edge_bps(buy_px, sell_px)
        if bps >= min_edge_bps:
            cands.append((
                PlanLeg(PositionDirection.Long,  buy_px,  base_sz, LegKind.Take),
                PlanLeg(PositionDirection.Short, sell_px, base_sz, LegKind.Take),
                bps, "TT"
            ))

    # MT：takerBid ↔ makerAsk（MAKE 卖给 taker 买；TAKE 买 maker 卖）
    if taker_bid and maker_ask:
        buy_px, sell_px = maker_ask.price, taker_bid.price
        bps = edge_bps(buy_px, sell_px)
        if bps >= min_edge_bps:
            cands.append((
                PlanLeg(PositionDirection.Long,  buy_px,  base_sz, LegKind.Take),
                PlanLeg(PositionDirection.Short, sell_px, base_sz, LegKind.Make),
                bps, "MT"
            ))

    # TM：makerBid ↔ takerAsk（TAKE 卖给 maker 买；MAKE 买接 taker 卖）
    if maker_bid and taker_ask:
        buy_px, sell_px = taker_ask.price, maker_bid.price
        bps = edge_bps(buy_px, sell_px)
        if bps >= min_edge_bps:
            cands.append((
                PlanLeg(PositionDirection.Long,  buy_px,  base_sz, LegKind.Make),
                PlanLeg(PositionDirection.Short, sell_px, base_sz, LegKind.Take),
                bps, "TM"
            ))

    if not cands:
        return None
    cands.sort(key=lambda x: x[2], reverse=True)
    return cands[0]

def keypair_from_json_env(var: str) -> Keypair:
    arr = json.loads(os.environ[var])
    return Keypair.from_secret_key(bytes(arr))

async def build_remaining_accounts() -> List[AccountMeta]:
    """
    Drift CPI 所需的 remaining accounts：
      - PerpMarket PDA
      - Oracle
      - Quote SpotMarket
      - （如用 MAKE/JIT）拍卖 taker 的 User(+Stats) —— 视需要追加
    """
    req = [
        "DRIFT_PERP_MARKET",
        "DRIFT_ORACLE",
        "DRIFT_QUOTE_SPOT_MARKET",
    ]
    metas: List[AccountMeta] = []
    for k in req:
        v = os.environ.get(k)
        if v:
            metas.append(AccountMeta(pubkey=PublicKey(v), is_signer=False, is_writable=False))
    # 可在此处按需 append 拍卖 taker 的 user/stats
    # for u in (os.environ.get("MAKER_USERS","").split(",")):
    #     if u: metas.append(AccountMeta(pubkey=PublicKey(u), is_signer=False, is_writable=False))
    return metas

# ===== Sniper 风格的“只在命中时出手”的双腿配对策略守护进程 =====
class JitterPairArb:
    def __init__(self, quote_source: QuoteSource):
        self.qs = quote_source

        self.rpc = os.environ["RPC_URL"]
        self.program_id = PublicKey(os.environ["JIT_PROXY_PROGRAM_ID"])
        self.drift_program_id = PublicKey(os.environ["DRIFT_PROGRAM_ID"])
        self.wallet = keypair_from_json_env("WALLET_KEYPAIR_JSON")
        self.market_index = int(os.environ.get("MARKET_INDEX", "0"))
        self.min_edge_bps = int(os.environ.get("MIN_EDGE_BPS", "6"))
        self.base_sz = int(os.environ.get("BASE_SZ", str(int(1e9))))
        self.loop_interval_ms = int(os.environ.get("LOOP_INTERVAL_MS", "200"))
        self.idl_path = os.environ.get("IDL_PATH", "target/idl/jit_proxy.json")

        self.client = AsyncClient(self.rpc, commitment=Confirmed)
        self.provider = Provider(self.client, Wallet(self.wallet))
        with open(self.idl_path, "r") as f:
            idl_json = json.load(f)
        self.program = Program(Idl.from_json(idl_json), self.program_id, self.provider)

    async def close(self):
        await self.client.close()

    async def _one_try(self) -> Optional[str]:
        # 1) 拉取 best quotes
        maker_bid = await self.qs.best_maker_bid()
        maker_ask = await self.qs.best_maker_ask()
        taker_bid = await self.qs.best_taker_bid()
        taker_ask = await self.qs.best_taker_ask()

        plan = pick_best_plan(maker_bid, maker_ask, taker_bid, taker_ask, self.base_sz, self.min_edge_bps)
        if not plan:
            return None
        first, second, bps, tag = plan

        # 2) 组参数与账户
        args = {
            "market_index": self.market_index,
            "min_edge_bps": self.min_edge_bps,
            "first": {
                "direction": first.direction, "price": first.price,
                "base_sz": first.base_sz,     "kind":  first.kind
            },
            "second": {
                "direction": second.direction, "price": second.price,
                "base_sz": second.base_sz,     "kind":  second.kind
            },
        }
        accounts = {
            "state":        PublicKey(os.environ["DRIFT_STATE"]),
            "user":         PublicKey(os.environ["DRIFT_USER"]),
            "userStats":    PublicKey(os.environ["DRIFT_USER_STATS"]),
            "authority":    self.wallet.public_key,
            "driftProgram": self.drift_program_id,
        }
        remaining = await build_remaining_accounts()

        # 3) 构造指令（只打一笔，Sniper 风格）
        ix = await self.program.instruction["arb_perp_plan"](args, Context(
            accounts=accounts,
            remaining_accounts=remaining
        ))

        # 4) 先 simulate（不盈利不广播）
        tx = Transaction()
        tx.add(ix)
        tx.fee_payer = self.wallet.public_key
        latest = await self.client.get_latest_blockhash()
        tx.recent_blockhash = Blockhash(latest.value.blockhash)
        tx.sign(self.wallet)

        sim = await self.client.simulate_transaction(tx, sig_verify=True, commitment=Processed)
        if sim.value.err is not None:
            # 失败（包括链上护栏触发/账户不全/太慢被别人填走等）
            return None

        # 5) 通过才发送
        sent = await self.client.send_raw_transaction(tx.serialize(), opts=TxOpts(skip_preflight=True))
        sig = sent.value
        print(f"[sent {tag} bps={bps}] {sig}")
        return sig

    async def run_forever(self):
        # Sniper：固定频率扫描，命中就打一笔
        interval = max(1, self.loop_interval_ms) / 1000.0
        backoff = 0.05
        while True:
            t0 = time.time()
            try:
                _ = await self._one_try()
                wait = max(0.0, interval - (time.time() - t0))
                await asyncio.sleep(wait)
                backoff = 0.05
            except Exception as e:
                print("[pair_arb] error:", repr(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 1.0)

# ===== 可执行入口（把 EnvQuoteSource 换成你的真实数据源适配）=====
async def _main():
    daemon = JitterPairArb(EnvQuoteSource())
    try:
        await daemon.run_forever()
    finally:
        await daemon.close()

if __name__ == "__main__":
    asyncio.run(_main())
