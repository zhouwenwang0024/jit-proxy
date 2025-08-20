# python/sdk/jit_proxy/jitter/jitter_sniper.py
# -*- coding: utf-8 -*-
#
# 目标：长期运行的“狙击”脚本，直接驱动链上的 programs/jit-proxy/src/instructions/arb_perp.rs
# 原则：每轮循环只“试探”一次——先 simulate 调用 arb_perp，成功才广播；链上仍有二次护栏（没交叉/不赚钱会回滚）。
#
# 必备环境变量（示例）：
#   RPC_URL=...
#   WALLET_KEYPAIR_JSON='[.., ..]'        # u8 数组 JSON
#   JIT_PROXY_PROGRAM_ID=...              # 你的 jit-proxy 程序 id
#   DRIFT_PROGRAM_ID=...                  # drift v2 程序 id
#   DRIFT_STATE=...
#   DRIFT_USER=...
#   DRIFT_USER_STATS=...
#   DRIFT_PERP_MARKET=...                 # 目标 market 的 PerpMarket PDA
#   DRIFT_ORACLE=...                      # 对应 oracle
#   DRIFT_QUOTE_SPOT_MARKET=...           # 一般是 spot 0 (USDC)
#   MARKET_INDEX=0
#   LOOP_INTERVAL_MS=250                  # 轮询间隔
#   COOLDOWN_MS_ON_SUCCESS=800            # 成功后冷却
#   CU_LIMIT=1200000                      # 可选：Compute Unit 限额
#   CU_PRICE_MICROLAMPORTS=0              # 可选：优先费（微 lamports）
#   MAKER_USERS=pubkey1,pubkey2           # 可选：让链上只在这些 maker 集合内找最优价（提高命中率）
#   MAKER_USER_STATS=pubkey1s,pubkey2s    # 与 MAKER_USERS 一一对应
#   IDL_PATH=target/idl/jit_proxy.json
#
import os
import json
import time
import asyncio
from typing import List, Optional

from solana.publickey import PublicKey
from solana.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Processed
from solana.transaction import Transaction, AccountMeta
from solana.blockhash import Blockhash
from solana.rpc.types import TxOpts

from anchorpy import Provider, Wallet, Program, Context, Idl

# ---------- 工具：读取密钥 ----------
def keypair_from_json_env(var: str) -> Keypair:
    arr = json.loads(os.environ[var])
    return Keypair.from_secret_key(bytes(arr))

# ---------- 工具：Compute Budget（可选） ----------
def build_compute_budget_ixs() -> List:
    ixs = []
    try:
        # 仅当装了 solders compute_budget 时启用；否则静默跳过
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

# ---------- 关键：把必需账户塞进 remaining_accounts ----------
def build_remaining_accounts_from_env() -> List[AccountMeta]:
    metas: List[AccountMeta] = []
    req_keys = [
        "DRIFT_PERP_MARKET",
        "DRIFT_ORACLE",
        "DRIFT_QUOTE_SPOT_MARKET",
    ]
    for k in req_keys:
        v = os.environ.get(k)
        if not v:
            raise RuntimeError(f"missing env {k}")
        metas.append(AccountMeta(pubkey=PublicKey(v), is_signer=False, is_writable=False))

    # 可选：指定一批 maker 的 User & UserStats，让链上只在这些用户里找 best bid/ask
    mus = [x for x in os.environ.get("MAKER_USERS", "").split(",") if x]
    mss = [x for x in os.environ.get("MAKER_USER_STATS", "").split(",") if x]
    if mus and mss and len(mus) == len(mss):
        for u in mus:
            metas.append(AccountMeta(pubkey=PublicKey(u), is_signer=False, is_writable=False))
        for s in mss:
            metas.append(AccountMeta(pubkey=PublicKey(s), is_signer=False, is_writable=False))
    return metas

# ---------- 主体 ----------
class ArbPerpSniper:
    def __init__(self):
        # 环境参数
        self.rpc = os.environ["RPC_URL"]
        self.program_id = PublicKey(os.environ["JIT_PROXY_PROGRAM_ID"])
        self.drift_program_id = PublicKey(os.environ["DRIFT_PROGRAM_ID"])
        self.wallet = keypair_from_json_env("WALLET_KEYPAIR_JSON")
        self.market_index = int(os.environ.get("MARKET_INDEX", "0"))
        self.loop_interval_ms = int(os.environ.get("LOOP_INTERVAL_MS", "250"))
        self.cooldown_ms_on_success = int(os.environ.get("COOLDOWN_MS_ON_SUCCESS", "800"))
        idl_path = os.environ.get("IDL_PATH", "target/idl/jit_proxy.json")

        # 连接 & Program
        self.client = AsyncClient(self.rpc, commitment=Confirmed)
        self.provider = Provider(self.client, Wallet(self.wallet))

        with open(idl_path, "r") as f:
            idl_json = json.load(f)
        self.program = Program(Idl.from_json(idl_json), self.program_id, self.provider)

        # 固定账户（Anchor Accounts）
        self.accounts = {
            "state":        PublicKey(os.environ["DRIFT_STATE"]),
            "user":         PublicKey(os.environ["DRIFT_USER"]),
            "userStats":    PublicKey(os.environ["DRIFT_USER_STATS"]),
            "authority":    self.wallet.public_key,
            "driftProgram": self.drift_program_id,
        }

        self.remaining_accounts = build_remaining_accounts_from_env()

    async def close(self):
        await self.client.close()

    async def run_forever(self):
        interval = max(1, self.loop_interval_ms) / 1000.0
        backoff = 0.05
        while True:
            t0 = time.time()
            try:
                ok = await self.try_once()
                # 成功后适当冷却，避免重复吃到同一撮合
                sleep_s = (self.cooldown_ms_on_success / 1000.0) if ok else max(0.0, interval - (time.time() - t0))
                await asyncio.sleep(sleep_s)
                backoff = 0.05
            except Exception as e:
                print("[arb_perp_sniper] error:", repr(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 1.0)

    async def try_once(self) -> bool:
        """
        单次尝试：
        1) 构造 arb_perp 指令（只带 market_index）
        2) 先 simulate （不盈利/不交叉 -> 链上护栏拦截，仿真失败，不广播）
        3) 通过才 send_raw_transaction
        """
        # 1) 构造 ix
        ctx = Context(
            accounts=self.accounts,
            remaining_accounts=self.remaining_accounts
        )
        ix = await self.program.instruction["arb_perp"](self.market_index, ctx)

        # 2) 组交易 + 可选 CU/优先费 + simulate
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
            # 被链上护栏拦下（常见：无交叉、PnL<=0、账户不对、保证金不足等）——不广播
            # 如需排查可打印 sim.value.logs
            return False

        # 3) 通过才发
        sent = await self.client.send_raw_transaction(tx.serialize(), opts=TxOpts(skip_preflight=True))
        print(f"[arb_perp] sent {sent.value}")
        return True

async def _main():
    bot = ArbPerpSniper()
    try:
        await bot.run_forever()
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(_main())
