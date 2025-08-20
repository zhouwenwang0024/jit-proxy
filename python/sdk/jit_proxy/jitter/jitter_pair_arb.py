# -*- coding: utf-8 -*-
#
# 实盘版 Pair Arb（RESTING↔RESTING，TAKE+TAKE）：自动发现并携带 maker 用户账户，调用链上 arb_perp 原子套利
#
# 依赖：
#   pip install -U anchorpy==0.16.0 solana==0.30.2 driftpy==0.5.15
#
# 必备准备：
#   1) anchor build 产出 IDL：target/idl/jit_proxy.json（其中有 "arb_perp" 指令）
#   2) .env（见本文档末尾模板），加载后再运行
#
# 运行：
#   export $(grep -v '^#' .env | xargs)
#   python python/sdk/jit_proxy/jitter/jitter_pair_arb.py
#
import os
import json
import time
import asyncio
from typing import List, Optional, Tuple, Dict
from collections import deque

from solana.publickey import PublicKey
from solana.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Processed
from solana.transaction import Transaction, AccountMeta
from solana.blockhash import Blockhash
from solana.rpc.types import TxOpts

from anchorpy import Provider, Wallet, Program, Context, Idl

# driftpy
from driftpy.drift_client import DriftClient
from driftpy.account_subscription_config import AccountSubscriptionConfig
from driftpy.events.event_subscriber import EventSubscriber
from driftpy.events.types import EventSubscriptionOptions, WebsocketLogProviderConfig


# ---------------- 工具 ----------------
def keypair_from_json_env(var: str) -> Keypair:
    arr = json.loads(os.environ[var])
    return Keypair.from_secret_key(bytes(arr))


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


# ---------------- 市场账户自动解析 ----------------
async def resolve_market_accounts(
    rpc_url: str,
    drift_program_id: PublicKey,
    market_index: int,
) -> Tuple[PublicKey, PublicKey, PublicKey]:
    """
    返回 (perp_market_pk, oracle_pk, spot0_pk)
    全用 driftpy 解析，无需手填。
    """
    conn = AsyncClient(rpc_url, commitment=Processed)
    dummy = Keypair()
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
        perp_pk = await client.get_perp_market_public_key(market_index)
        perp_acc = await client.get_perp_market_account(market_index)
        if perp_acc is None:
            raise RuntimeError("perp market account not found")
        # oracle 字段在不同版本 driftpy 名称可能不同，优先 amm.oracle
        try:
            oracle_pk = PublicKey(str(perp_acc.amm.oracle))
        except Exception:
            oracle_pk = PublicKey(str(perp_acc.oracle))
        spot0_pk = await client.get_spot_market_public_key(0)
        return perp_pk, oracle_pk, spot0_pk
    finally:
        await client.unsubscribe()
        await conn.close()


# ---------------- 根据 User 推导 UserStats（自动） ----------------
async def derive_user_stats_for_user(
    rpc_url: str,
    drift_program_id: PublicKey,
    user_pk: PublicKey,
) -> PublicKey:
    """
    读取 User 的 authority，按 PDA 规则推导 UserStats。你无需手填。
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
        # 返回 (pubkey, account)
        res = await client.get_user_account_public_key_and_account(user_pk)
        if res is None:
            raise RuntimeError(f"user account not found: {str(user_pk)}")
        _, user_acc = res
        try:
            stats_pk = await client.get_user_stats_public_key(user_acc.authority)
        except Exception:
            # 兜底按 PDA 计算
            seeds = [b"user_stats", bytes(PublicKey(str(user_acc.authority)))]
            stats_pk, _ = PublicKey.find_program_address(seeds, drift_program_id)
        return stats_pk
    finally:
        await client.unsubscribe()
        await conn.close()


# ---------------- 实盘 Maker 池：订阅事件，自动收集最近活跃的 RESTING makers ----------------
class MakerPool:
    """
    订阅 Drift 程序事件（OrderRecord / OrderActionRecord），
    把“最近 X 秒内在指定市场挂出 RESTING 限价单的用户 User 公钥”加入池子。
    注意：arb_perp 只处理 RESTING↔RESTING；拍卖 taker(auction>0) 不是这里的目标。
    """

    def __init__(self, rpc_url: str, drift_program_id: PublicKey, market_index: int,
                 ttl_secs: int = 60, max_users: int = 16):
        self.rpc_url = rpc_url
        self.drift_program_id = drift_program_id
        self.market_index = market_index
        self.ttl_secs = ttl_secs
        self.max_users = max_users

        self.conn: Optional[AsyncClient] = None
        self.client: Optional[DriftClient] = None
        self.sub: Optional[EventSubscriber] = None

        # { user_str: last_seen_unix }
        self.active: Dict[str, int] = {}
        # 保留一个队列做回收
        self.q: deque[Tuple[str, int]] = deque()
        self._stop = False

    async def start(self):
        self.conn = AsyncClient(self.rpc_url, commitment=Processed)
        self.client = DriftClient(
            self.conn, Wallet(Keypair()), "mainnet",
            program_id=self.drift_program_id,
            account_subscription=AccountSubscriptionConfig("cached"),
        )
        await self.client.subscribe()
        opts = EventSubscriptionOptions(
            event_types=("OrderRecord", "OrderActionRecord"),
            max_tx=2048, max_events_per_type=2048,
            order_by="blockchain", order_dir="asc",
            commitment="processed",
            log_provider_config=WebsocketLogProviderConfig(),
        )
        self.sub = EventSubscriber(self.conn, self.client.program, opts)
        self.sub.subscribe()
        asyncio.create_task(self._pump())

    async def close(self):
        self._stop = True
        if self.sub:
            self.sub.unsubscribe()
        if self.client:
            await self.client.unsubscribe()
        if self.conn:
            await self.conn.close()

    async def _pump(self):
        while not self._stop:
            try:
                events = await self.sub.get_events()
                now = int(time.time())
                for ev in events:
                    name, data = ev.name, ev.data
                    if name != "OrderRecord":
                        continue
                    # 过滤：目标市场 + auctionDuration == 0 （resting 限价单）
                    try:
                        mkt = int(getattr(data, "marketIndex"))
                        if mkt != self.market_index:
                            continue
                        dur = int(getattr(data, "auctionDuration", 0))
                        if dur > 0:
                            continue  # JIT taker，不属于 resting
                        # 把下单的 User 放进池子
                        # 字段可能叫 user / userKey / userPubkey，做多重兜底
                        user_pk_str = None
                        for fn in ("user", "userKey", "userPubkey", "userId"):
                            v = getattr(data, fn, None)
                            if v is not None:
                                user_pk_str = str(v)
                                break
                        if user_pk_str is None:
                            continue
                        self.active[user_pk_str] = now
                        self.q.append((user_pk_str, now))
                        # 回收过期
                        while self.q and now - self.q[0][1] > self.ttl_secs:
                            u, ts = self.q.popleft()
                            if self.active.get(u) == ts:
                                self.active.pop(u, None)
                    except Exception:
                        continue
            except Exception:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.04)

    def pick_users(self) -> List[PublicKey]:
        """
        返回“最近活跃的若干 maker User”，用于这笔交易的 remaining_accounts。
        数量上限 max_users；按最近出现时间倒序挑选。
        """
        if not self.active:
            return []
        # 按 last_seen 降序
        users = sorted(self.active.items(), key=lambda kv: kv[1], reverse=True)
        users = users[: self.max_users]
        return [PublicKey(u) for (u, _) in users]


# ---------------- 构造 remaining_accounts（市场三件套 + 实盘 Maker 池） ----------------
async def build_remaining_accounts(
    rpc_url: str,
    drift_program_id: PublicKey,
    market_index: int,
    maker_pool: MakerPool,
) -> List[AccountMeta]:
    metas: List[AccountMeta] = []
    # 1) 市场三件套
    perp_pk, oracle_pk, spot0_pk = await resolve_market_accounts(rpc_url, drift_program_id, market_index)
    metas.append(AccountMeta(perp_pk, is_signer=False, is_writable=False))
    metas.append(AccountMeta(oracle_pk, is_signer=False, is_writable=False))
    metas.append(AccountMeta(spot0_pk, is_signer=False, is_writable=False))

    # 2) 实盘 Maker 池：自动携带最近活跃的 RESTING makers
    maker_users = maker_pool.pick_users()

    # 如果池子暂时为空，允许用白名单兜底（可选）
    if not maker_users:
        wl = [x for x in os.environ.get("MAKER_USERS", "").split(",") if x]
        maker_users = [PublicKey(x) for x in wl]

    # 附带每个 User 对应的 UserStats（自动推导）
    for upk in maker_users:
        try:
            stats_pk = await derive_user_stats_for_user(rpc_url, drift_program_id, upk)
            metas.append(AccountMeta(upk, is_signer=False, is_writable=False))
            metas.append(AccountMeta(stats_pk, is_signer=False, is_writable=False))
        except Exception as e:
            print(f"[warn] cannot derive user_stats for {str(upk)}: {e}")

    return metas


# ---------------- 主体：长期运行，simulate→send ----------------
class PairArbBot:
    def __init__(self):
        # 基础配置
        self.rpc = os.environ["RPC_URL"]
        self.program_id = PublicKey(os.environ["JIT_PROXY_PROGRAM_ID"])
        self.drift_program_id = PublicKey(os.environ["DRIFT_PROGRAM_ID"])
        self.wallet = keypair_from_json_env("WALLET_KEYPAIR_JSON")
        self.market_index = int(os.environ.get("MARKET_INDEX", "0"))

        self.loop_interval_ms = int(os.environ.get("LOOP_INTERVAL_MS", "250"))
        self.cooldown_ms_on_success = int(os.environ.get("COOLDOWN_MS_ON_SUCCESS", "800"))
        idl_path = os.environ.get("IDL_PATH", "target/idl/jit_proxy.json")

        # anchorpy program
        self.client = AsyncClient(self.rpc, commitment=Confirmed)
        self.provider = Provider(self.client, Wallet(self.wallet))
        with open(idl_path, "r") as f:
            idl_json = json.load(f)
        self.program = Program(Idl.from_json(idl_json), self.program_id, self.provider)

        # 固定账户
        self.accounts = {
            "state":        PublicKey(os.environ["DRIFT_STATE"]),
            "user":         PublicKey(os.environ["DRIFT_USER"]),
            "userStats":    PublicKey(os.environ["DRIFT_USER_STATS"]),
            "authority":    self.wallet.public_key,
            "driftProgram": self.drift_program_id,
        }

        # 实盘 maker 池（用事件订阅自动更新）
        ttl = int(os.environ.get("MAKER_TTL_SECS", "60"))
        maxn = int(os.environ.get("MAX_MAKERS", "16"))
        self.maker_pool = MakerPool(self.rpc, self.drift_program_id, self.market_index, ttl_secs=ttl, max_users=maxn)

        self._cached_ra: Optional[List[AccountMeta]] = None
        self._last_ra_key: Optional[str] = None  # 根据 maker 池哈希判断是否需要重建 RA

    async def close(self):
        await self.maker_pool.close()
        await self.client.close()

    def _maker_key(self) -> str:
        # 基于当前池里用户的集合生成一个简单 key，变化则重建 RA
        users = [str(pk) for pk in self.maker_pool.pick_users()]
        users.sort()
        return "|".join(users)

    async def _ensure_ra(self) -> List[AccountMeta]:
        key = self._maker_key()
        if (self._cached_ra is None) or (key != self._last_ra_key):
            self._cached_ra = await build_remaining_accounts(self.rpc, self.drift_program_id, self.market_index, self.maker_pool)
            self._last_ra_key = key
        return self._cached_ra

    async def try_once(self) -> bool:
        remaining = await self._ensure_ra()
        if len(remaining) < 3:
            # 市场三件套都没齐，直接跳过
            return False

        # 构造链上 arb_perp 指令
        ctx = Context(accounts=self.accounts, remaining_accounts=remaining)
        ix = await self.program.instruction["arb_perp"](self.market_index, ctx)

        # 组交易 + 可选算力/优先费 + simulate
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
            # 常见：NoBestBid/NoBestAsk（池子里的人撤单了或不含目标对手）
            #       NoArbOpportunity（价差消失）/ 保证金不足 等
            # print(sim.value.logs)  # 排障时打开
            return False

        sent = await self.client.send_raw_transaction(tx.serialize(), opts=TxOpts(skip_preflight=True))
        print(f"[arb_perp] sent {sent.value}")
        return True

    async def run_forever(self):
        # 启动 maker 池事件订阅
        await self.maker_pool.start()

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
                print("[pair_arb] error:", repr(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 1.0)


async def _main():
    bot = PairArbBot()
    try:
        await bot.run_forever()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(_main())
