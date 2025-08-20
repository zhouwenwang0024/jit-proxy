use anchor_lang::prelude::*;
use drift::controller::position::PositionDirection;
use drift::cpi::accounts::{PlaceAndMake, PlaceAndTake};
use drift::error::DriftResult;
use drift::instructions::optional_accounts::{load_maps, AccountMaps};
use drift::math::casting::Cast;
use drift::math::constants::{BASE_PRECISION, MARGIN_PRECISION_U128, QUOTE_PRECISION};
use drift::math::margin::MarginRequirementType;
use drift::math::safe_math::SafeMath;
use drift::program::Drift;
use drift::state::oracle::OraclePriceData;
use drift::state::state::State;
use drift::state::user::{MarketType, OrderTriggerCondition, OrderType, User, UserStats};
use drift::state::user_map::load_user_maps;
use drift::state::order_params::{OrderParams, OrderParamsBitFlag, PostOnlyParam};

use std::collections::BTreeSet;
use std::ops::Deref;

use crate::error::ErrorCode;

// —— 复用你仓库中一致的账户上下文（与 arb_perp.rs 相同）
#[derive(Accounts)]
pub struct ArbPerp<'info> {
    pub state: Box<Account<'info, State>>,
    #[account(mut)]
    pub user: AccountLoader<'info, User>,
    #[account(mut)]
    pub user_stats: AccountLoader<'info, UserStats>,
    pub authority: Signer<'info>,
    pub drift_program: Program<'info, Drift>,
}

// —— 两腿类型：MAKE=place_and_make（JIT maker），TAKE=place_and_take（IOC）
#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, PartialEq, Eq)]
pub enum LegKind {
    Make = 0,
    Take = 1,
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy)]
pub struct PlanLeg {
    pub direction: PositionDirection, // Long=买, Short=卖
    pub price: u64,                   // PRICE_PRECISION
    pub base_sz: u64,                 // BASE_PRECISION
    pub kind: LegKind,                // Make / Take
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone)]
pub struct ArbPlanArgs {
    pub market_index: u16,
    pub min_edge_bps: i64,  // “不盈利不广播”的链上护栏
    pub first: PlanLeg,
    pub second: PlanLeg,
}

/// 执行两腿“计划”：链下先分拣+挑选最优，链上只做护栏+执行
pub fn arb_perp_plan<'c: 'info, 'info>(
    ctx: Context<'_, '_, 'c, 'info, ArbPerp<'info>>,
    args: ArbPlanArgs,
) -> Result<()> {
    let clock = Clock::get()?;
    let slot = clock.slot;

    // —— 初始仓位快照
    let taker = ctx.accounts.user.load()?;
    let (base_init, quote_init) = taker
        .get_perp_position(args.market_index)
        .map_or((0, 0), |p| (p.base_asset_amount, p.quote_asset_amount));

    // —— 加载必要账户（和原 arb_perp.rs 一致）
    let remaining_accounts_iter = &mut ctx.remaining_accounts.iter().peekable();
    let AccountMaps {
        perp_market_map,
        mut oracle_map,
        spot_market_map,
    } = load_maps(
        remaining_accounts_iter,
        &BTreeSet::new(),
        &BTreeSet::new(),
        slot,
        None,
    )?;

    // —— 估算可用保证金（与原逻辑一致）
    let quote_asset_token_amount = taker
        .get_quote_spot_position()
        .get_token_amount(spot_market_map.get_quote_spot_market()?.deref())?;

    // —— 市场/预言机
    let perp_market = perp_market_map.get_ref(&args.market_index)?;
    let oracle_price_data = oracle_map.get_price_data(&perp_market.oracle_id())?;

    // —— 链上护栏①：边际 bps（由两腿价格推出，买价/卖价与方向无关）
    // buy_px = Long腿的价格，sell_px = Short腿的价格
    let buy_px = if args.first.direction == PositionDirection::Long {
        args.first.price
    } else {
        args.second.price
    };
    let sell_px = if args.first.direction == PositionDirection::Short {
        args.first.price
    } else {
        args.second.price
    };
    require!(sell_px > buy_px, ErrorCode::NoArbOpportunity);
    let edge_bps = (((sell_px as i128 - buy_px as i128) * 10_000) / (buy_px as i128)) as i64;
    require!(edge_bps >= args.min_edge_bps, ErrorCode::NoArbOpportunity);

    // —— 尺寸：两腿取同一 base_sz（由链下已经决定），但仍做保证金上限约束
    let plan_sz = args.first.base_sz.min(args.second.base_sz);
    require!(plan_sz > 0, ErrorCode::NoArbOpportunity);

    // —— 方向一致性：两腿应一多一空
    require!(
        args.first.direction != args.second.direction,
        ErrorCode::NoArbOpportunity
    );

    // —— 保证金上限估算（与原函数相同）
    let intermediate_base = if args.first.direction == PositionDirection::Long {
        base_init.safe_add(plan_sz.cast()?)?
    } else {
        base_init.safe_sub(plan_sz.cast()?)?
    };
    let init_margin_ratio = perp_market.get_margin_ratio(
        intermediate_base.unsigned_abs().cast()?,
        MarginRequirementType::Initial,
        taker.is_high_leverage_mode(MarginRequirementType::Initial),
    )?;
    let max_by_margin = calculate_max_base_asset_amount(
        quote_asset_token_amount,
        init_margin_ratio,
        oracle_price_data,
    )?;
    let base_asset_amount = plan_sz
        .min(max_by_margin.cast()?)
        .max(perp_market.amm.min_order_size);

    drop(taker);
    drop(perp_market);

    // —— 构造 OrderParams
    let op = |direction: PositionDirection, price: u64, kind: LegKind| -> OrderParams {
        OrderParams {
            order_type: OrderType::Limit,
            market_type: MarketType::Perp,
            direction,
            user_order_id: 0,
            base_asset_amount,
            price,
            market_index: args.market_index,
            reduce_only: false,
            post_only: PostOnlyParam::None,
            bit_flags: match kind {
                LegKind::Make => 0, // 不 IOC
                LegKind::Take => OrderParamsBitFlag::ImmediateOrCancel as u8, // IOC
            },
            max_ts: None,
            trigger_price: None,
            trigger_condition: OrderTriggerCondition::Above,
            oracle_price_offset: None,
            auction_duration: None,
            auction_start_price: None,
            auction_end_price: None,
        }
    };

    // —— CPI 执行两腿
    let exec = |ctx: &Context<'_, '_, '_, 'info, ArbPerp<'info>>,
                leg: &PlanLeg| -> Result<()> {
        let drift_program = ctx.accounts.drift_program.to_account_info().clone();
        match leg.kind {
            LegKind::Make => {
                let cpi_accounts = PlaceAndMake {
                    state: ctx.accounts.state.to_account_info().clone(),
                    user: ctx.accounts.user.to_account_info().clone(),
                    user_stats: ctx.accounts.user_stats.to_account_info().clone(),
                    authority: ctx.accounts.authority.to_account_info().clone(),
                };
                let cpi_ctx = CpiContext::new(drift_program, cpi_accounts)
                    .with_remaining_accounts(ctx.remaining_accounts.into());
                drift::cpi::place_and_make_perp_order(cpi_ctx, op(leg.direction, leg.price, LegKind::Make), None)
            }
            LegKind::Take => {
                let cpi_accounts = PlaceAndTake {
                    state: ctx.accounts.state.to_account_info().clone(),
                    user: ctx.accounts.user.to_account_info().clone(),
                    user_stats: ctx.accounts.user_stats.to_account_info().clone(),
                    authority: ctx.accounts.authority.to_account_info().clone(),
                };
                let cpi_ctx = CpiContext::new(drift_program, cpi_accounts)
                    .with_remaining_accounts(ctx.remaining_accounts.into());
                drift::cpi::place_and_take_perp_order(cpi_ctx, op(leg.direction, leg.price, LegKind::Take), None)
            }
        }
    };

    exec(&ctx, &args.first)?;
    exec(&ctx, &args.second)?;

    // —— 链上护栏②：执行后“中性 + 正 PnL”
    let taker = ctx.accounts.user.load()?;
    let (base_end, quote_end) = taker
        .get_perp_position(args.market_index)
        .map_or((0, 0), |p| (p.base_asset_amount, p.quote_asset_amount));

    if base_end != base_init || quote_end <= quote_init {
        msg!(
            "base_end {} base_init {} quote_end {} quote_init {}",
            base_end, base_init, quote_end, quote_init
        );
        return Err(ErrorCode::NoArbOpportunity.into());
    }
    msg!("pnl {}", quote_end - quote_init);

    Ok(())
}

// —— 工具函数（与你原文件一致）
fn calculate_max_base_asset_amount(
    quote_asset_token_amount: u128,
    init_margin_ratio: u32,
    oracle_price_data: &OraclePriceData,
) -> DriftResult<u128> {
    quote_asset_token_amount
        .saturating_sub((quote_asset_token_amount / 100).min(10 * QUOTE_PRECISION))
        .safe_mul(MARGIN_PRECISION_U128)?
        .safe_div(init_margin_ratio.cast()?)?
        .safe_mul(BASE_PRECISION)?
        .safe_div(oracle_price_data.price.cast()?)
}
