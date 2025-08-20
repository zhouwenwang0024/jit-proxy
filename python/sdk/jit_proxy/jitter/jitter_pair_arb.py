# python/sdk/jit_proxy/jitter/jitter_sniper.py
# -*- coding: utf-8 -*-
#
# 目标：长期运行的“狙击”脚本，直接调用链上 programs/jit-proxy/src/instructions/arb_perp.rs（TAKE+TAKE 原子套利）。
# 原则：先 simulate 成功才广播；链上还会复核交叉/中性/正PnL——不盈利不落账。
#
# 依赖：
#   pip install anchorpy==0.16.0 solana==0.30.2 driftpy==0.5.15
#
# 必备环境变量（示例，按需替换）：
#   RPC_URL=https://api.mainnet-beta.solana.com
#   WALLET_KEYPAIR_JSON='[12,34, ...]'
#   JIT_PROXY_PROGRAM_ID=<你的 jit-proxy 程序ID>
#   DRIFT_PROGRAM_ID=<Drift v2 程序ID>
#   DRIFT_STATE=<state 公钥>
#   DRIFT_USER=<你的 user 公钥>
#   DRIFT_USER_STATS=<你的 user_stats 公钥>
#   MARKET_INDEX=0
#   IDL_PATH=target/idl/jit_proxy.json
#
# 可选（回退白名单；若你未接入“最佳对手钩子”，用它）：
#   MAKER_USERS=UserPk1,UserPk2,UserPk3
#
# 可选（循环/优先费/算力）：
#   LOOP_INTERVAL_MS=250
#   COOLDOWN_MS_ON_SUCCESS=800
#   CU_LIMIT=1200000
#   CU_PRICE_MICROLAMPORTS=0
#
import os
import json
import time
import asyncio
from typing import List, Optional, Tuple

from solana.publickey import PublicKey
from solana.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Processed
from solana.transaction import Transaction, AccountMeta
from solana.blockhash import Blockhash
from solana.rpc.types import TxOpts

from anchorpy import Provider, Wallet, Program, Context, Idl

# 仅用于自动解析市场账户
from driftpy.drift_client import DriftClient
from driftpy.account_subscription_config import AccountSubscriptionConfig


# ---------- 工具：读取密钥 ----------
def keypair_from_json_env(var: str) -> Keypair:
    arr = json.loads(os.environ[var])
    return Keypair.from_secret_key(bytes(arr))


# ---------- Compute Budget（可选） ----------
def build_compute_budget_ixs():
    ixs = []
    try:
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
        cu = int(os.environ.get("CU_LIMIT", "0"))
        if cu > 0:
            ixs.append(set_compute_unit_limit(cu))
        cup = int(os.environ.get("CU_PRICE_MICROLAMPORTS", "0"))
        if cup > 0:
            ixs.append(set_compute_unit_price(cup))
    except Exception:
        pass
    return ixs


# ---------- 核心：自动解析“市场三件套” ----------
async def resolve_market_accounts(
    rpc_url: str,
    drift_program_id: PublicKey,
    market_index: int,
) -> Tuple[PublicKey, PublicKey, PublicKey]:
    """
    返回 (perp_market_pk, oracle_pk, spot0_pk)
    全部用 driftpy 自动解析，无需手填。
    """
    conn = AsyncClient(rpc_url, commitment=Processed)
    dummy = Keypair()  # driftpy 需要一个钱包对象，但这里不签名
    client = DriftClient(
        conn,
        Wallet(dummy),
        "mainnet",
        perp_market_indexes=[market_index],
        spot_market_indexes=[0],
        program_id=drift_program_id,
        account_subscription=AccountSubscriptionConfig("cached"),
    )
    await client.subscribe()
    try:
        perp_pk = await client.get_perp_market_public_key(market_index)  # driftpy 提供
        perp_acc = await client.get_perp_market_account(market_index)
        if perp_acc is None:
            raise RuntimeError("perp market account not found")
        # oracle：不同 driftpy 版本字段路径略有差异，优先 amm.oracle
        try:
            oracle_pk = PublicKey(str(perp_acc.amm.oracle))
        except Exception:
            oracle_pk = PublicKey(str(perp_acc.oracle))
        spot0_pk = await client.get_spot_market_public_key(0)
        return perp_pk, oracle_pk, spot0_pk
    finally:
        await client.unsubscribe()
        await conn.close()


# ---------- 自动推导 UserStats（只需给 User 公钥） ----------
async def derive_user_stats_for_user(
    rpc_url: str,
    drift_program_id: PublicKey,
    user_pk: PublicKey,
) -> PublicKey:
    """
    通过 User 账户读取 authority，然后按 PDA 规则推导 UserStats。
    不需要你手填 UserStats。
    """
    conn = AsyncClient(rpc_url, commitment=Processed)
    dummy = Keypair()
    client = DriftClient(
        conn, Wallet(dummy), "mainnet",
        program_id=drift_program_id,
        account_subscription=AccountSubscriptionConfig("cached"),
    )
    await client.subscribe()
    try:
        user_acc = await client.get_user_account_public_key_and_account(user_pk)
        if user_acc is None:
            raise RuntimeError(f"user account not found: {str(user_pk)}")
        # driftpy 提供的 helper（不同版本可能叫法不同）；这里按常见 PDA 规则 fallback
        try:
            stats_pk = await client.get_user_stats_public_key(user_acc[1].authority)
        except Exception:
            from anchorpy import Program
            prog = client.program
            seeds = [b"user_stats", bytes(PublicKey(str(user_acc[1].authority)))]
            stats_pk, _ = PublicKey.find_program_address(seeds, drift_program_id)
        return stats_pk
    finally:
        await client.unsubscribe()
        await conn.close()


# ---------- 你的监控层钩子（请在这里接入“最优对手”） ----------
def get_best_counterparties() -> Optional[Tuple[PublicKey, PublicKey]]:
    """
    返回 (best_bid_user, best_ask_user)，都为“User账户公钥”。
    你只需在你现有 sniper 监控中，把“此刻最优的 bid/ask 属于谁”通过任意方式喂给这里：
      - 你可以把它们写入环境变量 BEST_BID_USER / BEST_ASK_USER；
      - 或者把它们写入某个共享文件/队列（自行改写本函数读取）。
    如果暂时没有，就返回 None，代码会退化到 MAKER_USERS 白名单。
    """
    bb = os.environ.get("BEST_BID_USER")
    ba = os.environ.get("BEST_ASK_USER")
    if bb and ba:
        try:
            return PublicKey(bb), PublicKey(ba)
        except Exception:
            return None
    return None


# ---------- 构造 remaining_accounts ----------
async def build_remaining_accounts(
    rpc_url: str,
    drift_program_id: PublicKey,
    market_index: int,
) -> List[AccountMeta]:
    metas: List[AccountMeta] = []

    # 1) 市场三件套（自动解析）
    perp_pk, oracle_pk, spot0_pk = await resolve_market_accounts(rpc_url, drift_program_id, market_index)
    metas.append(AccountMeta(perp_pk, is_signer=False, is_writable=False))
    metas.append(AccountMeta(oracle_pk, is_signer=False, is_writable=False))
    metas.append(AccountMeta(spot0_pk, is_signer=False, is_writable=False))

    # 2) 对手账户（优先用“最优对手钩子”，否则退回白名单）
    best = get_best_counterparties()
    maker_users: List[PublicKey] = []
    if best:
        maker_users.extend(list(best))
    else:
        wl = [x for x in os.environ.get("MAKER_USERS", "").split(",") if x]
        maker_users.extend([PublicKey(x) for x in wl])

    # 自动推导每个 User 对应的 UserStats（无需你手填）
    for upk in maker_users:
        try:
            stats_pk = await derive_user_stats_for_user(rpc_url, drift_program_id, upk)
            metas.append(AccountMeta(upk, is_signer=False, is_writable=False))
            metas.append(AccountMeta(stats_pk, is_signer=False, is_writable=False))
        except Exception as e:
            # 某个 user 失败不致命；跳过
            print(f"[warn] cannot derive user_stats for {str(upk)}: {e}")

    return metas


# ---------- 主体 ----------
class ArbPerpSniper:
    def __init__(self):
        self.rpc = os.environ["RPC_URL"]
        self.program_id = PublicKey(os.environ["JIT_PROXY_PROGRAM_ID"])
        self.drift_program_id = PublicKey(os.environ["DRIFT_PROGRAM_ID"])
        self.wallet = keypair_from_json_env("WALLET_KEYPAIR_JSON")
        self.market_index = int(os.environ.get("MARKET_INDEX", "0"))
        self.loop_interval_ms = int(os.environ.get("LOOP_INTERVAL_MS", "250"))
        self.cooldown_ms_on_success = int(os.environ.get("COOLDOWN_MS_ON_SUCCESS", "800"))
        idl_path = os.environ.get("IDL_PATH", "target/idl/jit_proxy.json")

        self.client = AsyncClient(self.rpc, commitment=Confirmed)
        self.provider = Provider(self.client, Wallet(self.wallet))
        with open(idl_path, "r") as f:
            idl_json = json.load(f)
        self.program = Program(Idl.from_json(idl_json), self.program_id, self.provider)

        self.accounts = {
            "state":        PublicKey(os.environ["DRIFT_STATE"]),
            "user":         PublicKey(os.environ["DRIFT_USER"]),
            "userStats":    PublicKey(os.environ["DRIFT_USER_STATS"]),
            "authority":    self.wallet.public_key,
            "driftProgram": self.drift_program_id,
        }

        # 缓存一份 RA（第一次运行时生成；后续在“最优对手”变化时也会自动更新）
        self._cached_ra: Optional[List[AccountMeta]] = None
        self._last_counterparties: Optional[Tuple[str, str]] = None

    async def close(self):
        await self.client.close()

    async def _ensure_remaining_accounts(self) -> List[AccountMeta]:
        # 如果“最优对手”发生变化，重建 RA；否则复用缓存
        cur = get_best_counterparties()
        cur_key = None
        if cur:
            cur_key = (str(cur[0]), str(cur[1]))
        if (self._cached_ra is None) or (cur_key != self._last_counterparties):
            self._cached_ra = await build_remaining_accounts(self.rpc, self.drift_program_id, self.market_index)
            self._last_counterparties = cur_key
        return self._cached_ra

    async def try_once(self) -> bool:
        # 1) RA
        remaining = await self._ensure_remaining_accounts()

        # 2) 构造 arb_perp 指令
        ctx = Context(accounts=self.accounts, remaining_accounts=remaining)
        ix = await self.program.instruction["arb_perp"](self.market_index, ctx)

        # 3) 组交易 + 可选算力/优先费 + simulate
        tx = Transaction()
        for cb_ix in build_compute_budget_ixs():
            tx.add(cb_ix)
        tx.add(ix)
        tx.fee_payer = self.wallet.public_key
        latest = await self.client.get_latest_blockhash()
        tx.recent_blockhash = Blockhash(latest.value.blockhash)
        tx.sign(self.wallet)

        sim = await self.client.simulate_transaction(tx, sig_verify=True, commitment=Processed)
        if sim.value.err is not None:
            # 常见：NoBestBid/NoBestAsk（对手撤单或 RA 不含该对手）
            #       NoArbOpportunity（交叉消失）/ 保证金不足等
            # 如要细查，可打印 sim.value.logs
            # print(sim.value.logs)
            return False

        # 4) 通过才发
        sent = await self.client.send_raw_transaction(tx.serialize(), opts=TxOpts(skip_preflight=True))
        print(f"[arb_perp] sent {sent.value}")
        return True

    async def run_forever(self):
        interval = max(1, self.loop_interval_ms) / 1000.0
        backoff = 0.05
        while True:
            t0 = time.time()
            try:
                ok = await self.try_once()
                sleep_s = (self.cooldown_ms_on_success / 1000.0) if ok \
                    else max(0.0, interval - (time.time() - t0))
                await asyncio.sleep(sleep_s)
                backoff = 0.05
            except Exception as e:
                print("[arb_perp_sniper] error:", repr(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 1.0)


async def _main():
    bot = ArbPerpSniper()
    try:
        await bot.run_forever()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(_main())
