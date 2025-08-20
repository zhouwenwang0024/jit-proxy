#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
arb_perp_bot.py
纯 taker：调用 JIT-Proxy 的 `arb_perp` 指令做“同一市场两腿 IOC 吃单套利”。

依赖:
  pip install -U driftpy anchorpy solana solders python-dotenv

环境变量:
  RPC_URL=...
  PRIVATE_KEY_JSON='[..]' 或 KEYPAIR_PATH=~/.config/solana/id.json

示例:
  python arb_perp_bot.py --env mainnet --market-index 5 --min-cross-bps 2 --min-size 0.001
"""

import os
import json
import asyncio
import argparse
from typing import List, Tuple, Set, Optional, Any

from dotenv import load_dotenv

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction as SInstruction
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.transaction import Transaction, TransactionInstruction

from anchorpy import Program, Provider, Wallet, Idl

from driftpy.config import configs
from driftpy.drift_client import DriftClient
from driftpy.dlob.dlob_subscriber import DLOBSubscriber
from driftpy.dlob.client_types import DLOBClientConfig
from driftpy.slot.slot_subscriber import SlotSubscriber

# ---------- 常量 ----------
JIT_PROXY_PROGRAM_ID = Pubkey.from_string("J1TnP8zvVxbtF5KFp5xRmWuvG9McnhzmBd9XGfCyuxFP")
DRIFT_PROGRAM_ID = Pubkey.from_string("dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH")

# ---------- 工具 ----------
def load_keypair() -> Keypair:
    p = os.getenv("KEYPAIR_PATH", "")
    j = os.getenv("PRIVATE_KEY_JSON", "")
    if j:
        return Keypair.from_bytes(bytes(json.loads(j)))
    if p and os.path.exists(p):
        with open(p, "r") as f:
            return Keypair.from_bytes(bytes(json.load(f)))
    raise RuntimeError("请设置 PRIVATE_KEY_JSON 或 KEYPAIR_PATH")

def bps(x: float) -> float:
    return x * 1e4

def _extract_pubkey_maybe(obj: Any, name_hint: str) -> Optional[Pubkey]:
    """
    尝试从 driftpy 返回的 account 对象上抽取 pubkey：
    支持 .public_key / .pubkey / .key / ["publicKey"] 等常见写法。
    """
    # 属性形式
    for attr in ("public_key", "pubkey", "key"):
        pk = getattr(obj, attr, None)
        if pk is not None:
            try:
                return Pubkey.from_string(str(pk))
            except Exception:
                pass
    # 字典形式
    if isinstance(obj, dict):
        for k in ("publicKey", "pubkey", "key"):
            if k in obj:
                try:
                    return Pubkey.from_string(str(obj[k]))
                except Exception:
                    pass
    return None

# ---------- 账户收集（remaining_accounts） ----------
async def gather_market_and_oracle_accounts(
    drift_client: DriftClient, market_index: int
) -> Tuple[Pubkey, Pubkey, Pubkey]:
    """
    返回 (perp_market_account, oracle_account, quote_spot_market_account)
    多路径尝试拿 pubkey，拿不到直接报错。
    """
    pm = await drift_client.get_perp_market_account(market_index)
    perp_market_pk = _extract_pubkey_maybe(pm, "perp_market")
    if perp_market_pk is None:
        raise RuntimeError("无法从 perp market account 抽取公钥（pubkey/public_key 缺失）")

    oracle_pk_raw = getattr(pm, "oracle", None) or (pm.get("oracle") if isinstance(pm, dict) else None)
    if oracle_pk_raw is None:
        raise RuntimeError("perp market account 缺少 oracle 字段")
    oracle_pk = Pubkey.from_string(str(oracle_pk_raw))

    quote_spot = await drift_client.get_quote_spot_market_account()
    quote_spot_pk = _extract_pubkey_maybe(quote_spot, "quote_spot")
    if quote_spot_pk is None:
        raise RuntimeError("无法从 quote spot market account 抽取公钥")

    return (perp_market_pk, oracle_pk, quote_spot_pk)

def _get_node_user_pubkey(node: Any) -> Optional[Pubkey]:
    """
    同时兼容对象/字典形式，尝试取出 maker 的 user account pubkey。
    常见字段：user / userAccount
    """
    # 对象形式
    upk = getattr(node, "user", None) or getattr(node, "userAccount", None)
    if upk is not None:
        try:
            return Pubkey.from_string(str(upk))
        except Exception:
            pass
    # 字典形式
    if isinstance(node, dict):
        for k in ("user", "userAccount"):
            if k in node and node[k] is not None:
                try:
                    return Pubkey.from_string(str(node[k]))
                except Exception:
                    pass
    return None

async def collect_maker_users_from_l2(
    dlob: DLOBSubscriber, market_index: int, max_users: int = 32, depth_per_side: int = 8
) -> List[Pubkey]:
    """
    从 L2 的前 N 档（每边 depth_per_side）收集 maker 的 user 公钥，去重并限流到 max_users。
    """
    l2 = dlob.get_l2_orderbook_sync(None, market_index=market_index)  # 默认 Perp
    bids = l2.get("bids") if isinstance(l2, dict) else getattr(l2, "bids", [])
    asks = l2.get("asks") if isinstance(l2, dict) else getattr(l2, "asks", [])

    users: List[Pubkey] = []
    seen: Set[str] = set()

    def _collect(side):
        cnt = 0
        for node in side:
            if cnt >= depth_per_side:
                break
            upk = _get_node_user_pubkey(node)
            if upk is None:
                continue
            key = str(upk)
            if key not in seen:
                users.append(upk)
                seen.add(key)
                cnt += 1

    _collect(bids or [])
    _collect(asks or [])

    # 限流
    if len(users) > max_users:
        users = users[:max_users]

    return users

# ---------- 机会判定 ----------
def estimate_cross_and_size_from_l2(l2) -> Tuple[float, float]:
    """
    用 L2 顶层 bid/ask 粗估交叉 bps 与可成交最小量（两边size的 min）。
    """
    bids = l2.get("bids") if isinstance(l2, dict) else getattr(l2, "bids", [])
    asks = l2.get("asks") if isinstance(l2, dict) else getattr(l2, "asks", [])
    if not bids or not asks:
        return (float("-inf"), 0.0)

    def _price(x):
        return x.get("price") if isinstance(x, dict) else getattr(x, "price", None)
    def _size(x):
        return x.get("baseAssetAmount") if isinstance(x, dict) else getattr(x, "baseAssetAmount", None)

    best_bid = float(_price(bids[0]) or 0.0)
    best_ask = float(_price(asks[0]) or 0.0)
    if best_bid <= 0 or best_ask <= 0:
        return (float("-inf"), 0.0)

    min_size = float(min(float(_size(bids[0]) or 0.0), float(_size(asks[0]) or 0.0)))
    cross_bps = bps((best_bid - best_ask) / ((best_bid + best_ask) / 2.0))
    return (cross_bps, min_size)

# ---------- 构造 & 发送 arb_perp 指令 ----------
async def _to_solana_instruction(ix: SInstruction) -> TransactionInstruction:
    """
    将 solders 的 Instruction 转成 solana-py 的 TransactionInstruction，便于合并到 Transaction。
    """
    return TransactionInstruction(
        keys=[{"pubkey": m.pubkey, "is_signer": m.is_signer, "is_writable": m.is_writable} for m in ix.accounts],
        program_id=ix.program_id,
        data=bytes(ix.data),
    )

async def send_arb_perp(
    provider: Provider,
    drift_client: DriftClient,
    program: Program,
    market_index: int,
    maker_users: List[Pubkey],
    cu_limit: int = 600_000,
    micro_lamports: int = 5_000,  # priority fee
    do_simulate: bool = True,
) -> str:
    """
    组装主账户 + remaining_accounts（市场/预言机/quote + maker users），调用 arb_perp(market_index)。
    交易前可选 simulate，提前发现账户/顺序问题。
    """
    kp = provider.wallet.payer

    # 主账户
    state_acct = await drift_client.get_state_account()
    state_pk = _extract_pubkey_maybe(state_acct, "state")
    if state_pk is None:
        raise RuntimeError("无法从 state account 抽取公钥")

    user_pk = await drift_client.get_user_account_public_key(kp.pubkey())
    user_stats_pk = await drift_client.get_user_stats_account_public_key(kp.pubkey())

    accounts = {
        "state": state_pk,
        "user": user_pk,
        "user_stats": user_stats_pk,
        "authority": kp.pubkey(),
        "drift_program": DRIFT_PROGRAM_ID,
    }

    # remaining_accounts: 先市场/预言机/quote，再 maker users
    perp_pk, oracle_pk, quote_spot_pk = await gather_market_and_oracle_accounts(drift_client, market_index)
    remaining = [
        {"pubkey": perp_pk, "is_signer": False, "is_writable": False},
        {"pubkey": oracle_pk, "is_signer": False, "is_writable": False},
        {"pubkey": quote_spot_pk, "is_signer": False, "is_writable": False},
    ] + [{"pubkey": u, "is_signer": False, "is_writable": False} for u in maker_users]

    # ComputeBudget（用 solders 生成，再转 solana-py）
    cu_ix_s = set_compute_unit_limit(cu_limit)
    cu_price_ix_s = set_compute_unit_price(micro_lamports)
    cu_ix = await _to_solana_instruction(SInstruction.from_bytes(cu_ix_s.to_bytes()))
    pri_ix = await _to_solana_instruction(SInstruction.from_bytes(cu_price_ix_s.to_bytes()))

    # 构造指令
    arb_ix = await program.methods.arb_perp(market_index).accounts(accounts).remaining_accounts(remaining).instruction()

    # 组交易
    tx = Transaction()
    tx.add(cu_ix)
    tx.add(pri_ix)
    tx.add(arb_ix)

    # 预模拟
    if do_simulate:
        sim = await provider.connection.simulate_transaction(tx, sig_verify=False)
        if sim.value.err is not None:
            raise RuntimeError(f"simulate 失败: {sim.value.err} | logs: {sim.value.logs}")

    # 发送
    sig = await provider.send(tx, opts=Confirmed)
    return sig

# ---------- IDL 加载 ----------
async def load_program_with_idl(provider: Provider, idl_path: Optional[str] = None) -> Program:
    """
    优先从链上拉 IDL；若失败且提供了 idl_path，则回退本地 IDL。
    """
    onchain_idl = await Program.fetch_idl(JIT_PROXY_PROGRAM_ID, provider)
    if onchain_idl is not None:
        return Program(onchain_idl, JIT_PROXY_PROGRAM_ID, provider)

    if idl_path:
        if not os.path.exists(idl_path):
            raise RuntimeError(f"指定的本地 IDL 不存在: {idl_path}")
        with open(idl_path, "r") as f:
            local = Idl.from_json(json.load(f))
        return Program(local, JIT_PROXY_PROGRAM_ID, provider)

    raise RuntimeError("无法获取 JIT-Proxy 的 IDL（链上无 IDL 且未提供 --idl-path）")

# ---------- 主流程 ----------
async def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", choices=["mainnet", "devnet"], default=os.getenv("DRIFT_ENV", "mainnet"))
    parser.add_argument("--market-index", type=int, required=True)
    parser.add_argument("--min-cross-bps", type=float, default=2.0, help="最小交叉（bps），默认 2 bps")
    parser.add_argument("--min-size", type=float, default=0.0, help="最小体量（合约基精度）")
    parser.add_argument("--max-makers", type=int, default=32, help="最大 maker 用户数量，过多可能超账户上限")
    parser.add_argument("--depth-per-side", type=int, default=8, help="每边提取多少档 maker")
    parser.add_argument("--cu-limit", type=int, default=600_000)
    parser.add_argument("--prio-udlamports", type=int, default=5_000)
    parser.add_argument("--idl-path", type=str, default=None, help="JIT-Proxy 本地 IDL JSON 路径（链上无 IDL 时兜底）")
    args = parser.parse_args()

    # 连接 & Provider
    rpc = os.getenv("RPC_URL") or (configs[args.env].default_http)
    kp = load_keypair()
    connection = AsyncClient(rpc)
    provider = Provider(connection, Wallet(kp))

    # Drift 客户端 & 订阅
    drift_client = DriftClient.from_config(configs[args.env], provider)
    await drift_client.subscribe()
    slot_sub = SlotSubscriber(drift_client.connection)
    await slot_sub.subscribe()
    dlob = DLOBSubscriber(DLOBClientConfig(drift_client, None, slot_sub, update_frequency=1000))
    await dlob.subscribe()
    await asyncio.sleep(0.8)  # 等第一帧

    # Program（IDL 载入，链上优先）
    program = await load_program_with_idl(provider, idl_path=args.idl_path)

    # 计算交叉与体量，链下预判
    l2 = dlob.get_l2_orderbook_sync(None, market_index=args.market_index)
    cross_bps, min_size = estimate_cross_and_size_from_l2(l2)
    print(f"[L2] cross≈{cross_bps:.4f} bps, min_size≈{min_size:.6f}")

    if cross_bps < args.min_cross_bps or min_size < args.min_size:
        print("⛔ 未满足链下交叉/体量阈值，不上链。")
        await connection.close()
        return

    # 收集 maker users（多档位，去重，限流）
    makers = await collect_maker_users_from_l2(
        dlob,
        args.market_index,
        max_users=args.max_makers,
        depth_per_side=args.depth_per_side,
    )
    if len(makers) < 2:
        print("⛔ maker 用户数太少，无法构建有效 L2（需要 bid/ask 两侧）。")
        await connection.close()
        return

    # 发送 arb_perp
    try:
        sig = await send_arb_perp(
            provider=provider,
            drift_client=drift_client,
            program=program,
            market_index=args.market_index,
            maker_users=makers,
            cu_limit=args.cu_limit,
            micro_lamports=args.prio_udlamports,
            do_simulate=True,
        )
        print(f"✅ 发送成功 txSig={sig}")
    except Exception as e:
        print(f"❌ 发送失败: {e}")

    await connection.close()

if __name__ == "__main__":
    asyncio.run(main())
