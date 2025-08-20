#!/usr/bin/env ts-node

/**
 * plan_orchestrator.ts
 * 链下分拣：把 taker/maker 候选价组合成最佳两腿计划（MAKE/TAKE + Long/Short + 价 + 量）
 * 流程：
 *   1) 采集候选：taker_best_bid/ask、maker_best_bid/ask（演示用接口留空/伪实现，你可接 DLOB/SDK）
 *   2) 枚举候选组合：TT / MT / TM，计算 bps
 *   3) 选择 bps 最大且 ≥ minEdgeBps
 *   4) 组装 ArbPlanArgs，simulate 成功才 send
 *
 * 环境：
 *   RPC_URL, WALLET_KEYPAIR(JSON数组), MARKET="SOL-PERP", MIN_EDGE_BPS, BASE_SZ
 *
 * 依赖：
 *   npm i @solana/web3.js @project-serum/anchor @drift-labs/sdk
 */

import {
  Connection, Keypair, PublicKey, ComputeBudgetProgram,
  TransactionMessage, VersionedTransaction, AddressLookupTableAccount
} from '@solana/web3.js';
import { BN } from '@drift-labs/sdk';
import { Program, AnchorProvider, Idl } from '@project-serum/anchor';

// ====== 根据你的部署更新这些常量 ======
const PROGRAM_ID = new PublicKey(process.env.JIT_PROXY_PROGRAM_ID!); // 你的 jit-proxy program id
const IDL: Idl = {
  "version": "0.1.0",
  "name": "jit_proxy",
  "instructions": [
    {
      "name": "arbPerpPlan",
      "accounts": [
        {"name":"state","isMut":false,"isSigner":false},
        {"name":"user","isMut":true,"isSigner":false},
        {"name":"userStats","isMut":true,"isSigner":false},
        {"name":"authority","isMut":false,"isSigner":true},
        {"name":"driftProgram","isMut":false,"isSigner":false}
      ],
      "args": [
        {
          "name": "args",
          "type": {
            "defined": "ArbPlanArgs"
          }
        }
      ]
    }
  ],
  "types": [
    {
      "name": "LegKind",
      "type": {"kind":"enum","variants":[{"name":"Make"},{"name":"Take"}]}
    },
    {
      "name": "PlanLeg",
      "type": {"kind":"struct","fields":[
        {"name":"direction","type":{"defined":"PositionDirection"}}, // 需与 on-chain 定义一致
        {"name":"price","type":"u64"},
        {"name":"base_sz","type":"u64"},
        {"name":"kind","type":{"defined":"LegKind"}}
      ]}
    },
    {
      "name":"ArbPlanArgs",
      "type":{"kind":"struct","fields":[
        {"name":"market_index","type":"u16"},
        {"name":"min_edge_bps","type":"i64"},
        {"name":"first","type":{"defined":"PlanLeg"}},
        {"name":"second","type":{"defined":"PlanLeg"}}
      ]}
    },
    // 你可以把 PositionDirection 也加入到 IDL types（此处简化处理）
  ]
} as unknown as Idl;

// ====== 简化方向枚举，需与链上 PositionDirection 对应 ======
enum PositionDirection { LONG = 0, SHORT = 1 }
enum LegKind { Make = 0, Take = 1 }

type Quote = { price: number; size: number; };

// —— 演示：从你的数据源取得最优价。你可以换成 DLOB / SDK 读簿 / 订阅流。
async function fetchMakerBestBidAsk(_market: string): Promise<{ bid?: Quote, ask?: Quote }> {
  // TODO: 接入真正的数据源
  return { bid: undefined, ask: undefined };
}
async function fetchTakerBestBidAsk(_market: string): Promise<{ bid?: Quote, ask?: Quote }> {
  // TODO: 接入真正的拍卖 taker 源（你手里通常有当前拍卖 taker 的账户 → 估出触发价）
  return { bid: undefined, ask: undefined };
}

// —— 计算边际 bps
function edgeBps(buyPx: number, sellPx: number) {
  if (sellPx <= buyPx) return -1;
  return Math.floor(((sellPx - buyPx) * 10_000) / buyPx);
}

// —— 把两腿参数序列化为链上 PlanLeg
function leg(direction: PositionDirection, price: number, baseSz: number, kind: LegKind) {
  return {
    direction,
    price: Math.round(price),        // 假设已是 PRICE_PRECISION 整数价
    base_sz: Math.round(baseSz),     // 假设已是 BASE_PRECISION 整数量
    kind
  };
}

async function main() {
  // ====== 基本配置 ======
  const rpc = process.env.RPC_URL!;
  const kp = Keypair.fromSecretKey(Uint8Array.from(JSON.parse(process.env.WALLET_KEYPAIR!)));
  const market = process.env.MARKET || "SOL-PERP";
  const minEdgeBps = Number(process.env.MIN_EDGE_BPS || 6);
  const baseSz = Number(process.env.BASE_SZ || 1e9); // 1.0 BASE_PRECISION 示例
  const marketIndex = Number(process.env.MARKET_INDEX || 0);

  const conn = new Connection(rpc, 'confirmed');
  const provider = new AnchorProvider(conn, { publicKey: kp.publicKey } as any, {});
  const program = new Program(IDL, PROGRAM_ID, provider);

  // ====== 采集候选最优价（请替换为你的真实来源）======
  const { bid: makerBid, ask: makerAsk } = await fetchMakerBestBidAsk(market);
  const { bid: takerBid, ask: takerAsk } = await fetchTakerBestBidAsk(market);

  // 枚举三种组合（TT / MT / TM），择优
  type Plan = {
    first: ReturnType<typeof leg>,    // 第一腿
    second: ReturnType<typeof leg>,   // 第二腿
    bps: number
  };
  const candidates: Plan[] = [];

  // TT：makerBid ↔ makerAsk（两腿 TAKE/IOC）
  if (makerBid && makerAsk) {
    const buyPx = makerAsk.price, sellPx = makerBid.price;
    const bps = edgeBps(buyPx, sellPx);
    if (bps >= 0) {
      // 方向选择：第一腿尽量朝“回零暴露”的方向，这里演示用固定顺序
      candidates.push({
        first:  leg(PositionDirection.LONG,  buyPx,  baseSz, LegKind.Take),
        second: leg(PositionDirection.SHORT, sellPx, baseSz, LegKind.Take),
        bps
      });
    }
  }

  // MT：takerBid ↔ makerAsk（我们 MAKE 卖给 taker 买单；我们 TAKE 买 maker 卖单）
  if (takerBid && makerAsk) {
    const buyPx = makerAsk.price, sellPx = takerBid.price;
    const bps = edgeBps(buyPx, sellPx);
    if (bps >= 0) {
      candidates.push({
        first:  leg(PositionDirection.LONG,  buyPx,  baseSz, LegKind.Take),
        second: leg(PositionDirection.SHORT, sellPx, baseSz, LegKind.Make),
        bps
      });
    }
  }

  // TM：makerBid ↔ takerAsk（我们 TAKE 卖给 maker 买单；我们 MAKE 买接住 taker 卖单）
  if (makerBid && takerAsk) {
    const buyPx = takerAsk.price, sellPx = makerBid.price;
    const bps = edgeBps(buyPx, sellPx);
    if (bps >= 0) {
      candidates.push({
        first:  leg(PositionDirection.LONG,  buyPx,  baseSz, LegKind.Make),
        second: leg(PositionDirection.SHORT, sellPx, baseSz, LegKind.Take),
        bps
      });
    }
  }

  // 选择边际最大且 ≥ minEdgeBps 的计划
  const best = candidates.reduce<Plan | undefined>((acc, p) => {
    if (p.bps < minEdgeBps) return acc;
    if (!acc || p.bps > acc.bps) return p;
    return acc;
  }, undefined);

  if (!best) {
    console.log(`[SKIP] no plan meets minEdgeBps=${minEdgeBps}`);
    return;
  }

  // ====== 组交易（compute budget + 调用 arb_perp_plan）======
  const latest = await conn.getLatestBlockhash();
  const cu = [
    ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 }),
    ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 1 }),
  ];

  const ix = await program.methods
    .arbPerpPlan({
      market_index: marketIndex,
      min_edge_bps: minEdgeBps,
      first:  best.first,
      second: best.second,
    } as any)
    .accounts({
      state:        new PublicKey(process.env.DRIFT_STATE!),
      user:         new PublicKey(process.env.DRIFT_USER!),
      userStats:    new PublicKey(process.env.DRIFT_USER_STATS!),
      authority:    kp.publicKey,
      driftProgram: new PublicKey(process.env.DRIFT_PROGRAM_ID!),
    })
    // 注意：这里需要 .remainingAccounts([...]) 把 PerpMarket / Oracle / QuoteSpot / (需要的 maker/taker User+Stats) 一并塞进去
    // .remainingAccounts(buildRemainingAccounts(...))
    .instruction();

  const msg = new TransactionMessage({
    payerKey: kp.publicKey,
    recentBlockhash: latest.blockhash,
    instructions: [...cu, ix],
  }).compileToV0Message([] as AddressLookupTableAccount[]);

  const tx = new VersionedTransaction(msg);
  tx.sign([kp]);

  // ====== 先 simulate（不盈利不广播）======
  const sim = await conn.simulateTransaction(tx, { sigVerify: true, replaceRecentBlockhash: true });
  if (sim.value.err) {
    console.log(`[BLOCKED] simulate err =`, sim.value.err);
    if (sim.value.logs) console.log(sim.value.logs.join('\n'));
    return;
  }

  // ====== 通过才发送 ======
  const sig = await conn.sendRawTransaction(tx.serialize(), { skipPreflight: true });
  console.log(`sent: ${sig}`);
}

main().catch(e => {
  console.error(e);
  process.exit(1);
});

