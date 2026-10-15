#!/usr/binpython
"""Central Candidate Pipeline for TRIO Options Trading Framework.

This module provides the end-to-end pipeline:
  Strategy signal
    -> Candidate creation
    -> Contract eligibility
    -> Contract selection
    -> Candidate ranking
    -> Portfolio risk approval
    -> Simulated trade lifecycle
    -> Trade record and performance accounting
"""

import sys
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path
sys.path.insert(0, '.')

# IST timezone
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# IMPORTS FROM PROJECT MODULES
# ---------------------------------------------------------------------------

from data_importer import (
    import_5min_index_candles,
    import_option_premium_candles,
    get_contract_specifications,
    validate_candle_data,
    validate_option_candle_data,
)

from contract_selector import (
    is_contract_eligible,
    select_option_contract,
    select_contract_by_moneyness,
    validate_expiry_against_underlying,
)
from option_data_schema import options_pnl_readiness, NOT_READY_FOR_OPTIONS_PNL

from experimental_risk import (
    init_experimental_risk,
    process_signal_with_risk_controls,
    calculate_experimental_position_size,
    check_daily_loss_limit,
    check_max_trades_per_day,
    check_consecutive_losses,
    check_time_cutoff_ist,
    check_duplicate_entry,
    check_max_open_positions,
    check_correlated_exposure,
    configure_experimental_risk,
    get_experimental_risk_state,
    reset_active_experimental_risk,
    record_experimental_trade_outcome,
    log_rejected_entry,
)


# ---------------------------------------------------------------------------
# CANDIDATE STRUCTURE
# ---------------------------------------------------------------------------

@dataclass
class OptionsCandidate:
    """Structured options candidate resulting from a strategy signal."""
    candidate_id: str
    signal_timestamp: str
    underlying_symbol: str
    strategy_name: str
    direction: str  # "bullish" or "bearish"
    option_type: str  # "CE" or "PE"
    entry_price: float
    stop_loss: float
    target: float
    signal_reason: str
    underlying_price_at_signal: float
    
    # Contract selection fields
    selected_contract: Optional[Dict[str, Any]] = None
    contract_eligibility: Optional[Tuple[bool, str]] = None
    contract_rank_score: float = 0.0
    
    # Risk approval fields
    risk_approved: bool = False
    risk_rejection_reason: Optional[str] = None
    position_size: int = 0
    daily_pnl_impact: float = 0.0
    
    # Trade lifecycle
    trade_executed: bool = False
    trade_id: Optional[str] = None
    realized_pnl: float = 0.0
    exchange: str = "NSE"

    # Option-premium execution and lifecycle fields.  Underlying signal
    # prices above remain separate from these contract prices.
    option_entry_premium: Optional[float] = None
    option_stop_premium: Optional[float] = None
    option_exit_premium: Optional[float] = None
    contract_lot_size: int = 0
    entry_bid: Optional[float] = None
    entry_ask: Optional[float] = None
    exit_timestamp: Optional[str] = None
    exit_reason: Optional[str] = None
    gross_pnl: float = 0.0
    transaction_costs: float = 0.0
    net_pnl: float = 0.0
    underlying_candles: Optional[List[Dict[str, Any]]] = None
    option_candles: Optional[List[Dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# CANDIDATE PIPELINE CLASS
# ---------------------------------------------------------------------------

class CandidatePipeline:
    """Central pipeline coordinating strategy signals through contract selection
    and portfolio risk approval."""
    
    def __init__(
        self,
        csv_candles_path: str,
        start_date: str = None,
        end_date: str = None,
        initial_equity: float = 500000.0,
        risk_per_trade_pct: float = 0.30,
        max_daily_loss_pct: float = 1.0,
        max_trades_per_day: int = 5,
        max_consecutive_losses: int = 2,
        time_cutoff_ist: str = "14:30",
        max_open_positions: int = 5,
        correlated_groups: Optional[List[List[str]]] = None,
        available_option_records: Optional[List[Dict[str, Any]]] = None,
        option_data_provenance: str = "unavailable",
    ):
        self.csv_candles_path = csv_candles_path
        self.start_date = start_date
        self.end_date = end_date
        self.initial_equity = initial_equity
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_trades_per_day = max_trades_per_day
        self.max_consecutive_losses = max_consecutive_losses
        self.time_cutoff_ist = time_cutoff_ist
        self.max_open_positions = max_open_positions
        self.correlated_groups = correlated_groups or []
        self.available_option_records = list(available_option_records or [])
        self.option_data_provenance = option_data_provenance
        
        # State
        self.candidates: List[OptionsCandidate] = []
        self.approved_trades: List[Dict[str, Any]] = []
        self.rejected_candidates: List[Dict[str, Any]] = []
        self.daily_trade_count: int = 0
        self.consecutive_losses: int = 0
        self.daily_pnl: float = 0.0
        self.high_watermark: float = 0.0
        
        # Initialize risk state
        init_experimental_risk(capital=self.initial_equity, starting_equity=self.initial_equity)
        configure_experimental_risk(
            max_trades_per_day=self.max_trades_per_day,
            max_consecutive_losses=self.max_consecutive_losses,
            max_open_positions=self.max_open_positions,
            time_cutoff_ist=self._parse_cutoff_time(self.time_cutoff_ist),
            correlated_groups=self.correlated_groups,
        )
        
        # Contract specifications
        self.contract_specs = get_contract_specifications("NIFTY")
        
        # Track trade IDs
        self.next_trade_id: int = 1

    @staticmethod
    def _parse_cutoff_time(value: str):
        from datetime import time as dtime
        if hasattr(value, "hour"):
            return value
        hour, minute = str(value).split(":", 1)[:2]
        return dtime(int(hour), int(minute))

    def add_candidate(self, candidate: OptionsCandidate) -> OptionsCandidate:
        """Add a structured candidate without pre-approving its contract."""
        candidate.contract_eligibility = None
        candidate.risk_approved = False
        self.candidates.append(candidate)
        return candidate
    
    def reset_pipeline(self) -> None:
        """Reset pipeline state for a new session/day."""
        self.candidates = []
        self.approved_trades = []
        self.rejected_candidates = []
        self.daily_trade_count = 0
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.high_watermark = 0.0
        self.next_trade_id = 1
        init_experimental_risk(
            capital=self.initial_equity,
            starting_equity=self.initial_equity,
        )
        configure_experimental_risk(
            max_trades_per_day=self.max_trades_per_day,
            max_consecutive_losses=self.max_consecutive_losses,
            max_open_positions=self.max_open_positions,
            time_cutoff_ist=self._parse_cutoff_time(self.time_cutoff_ist),
            correlated_groups=self.correlated_groups,
        )
    
    # ------------------------------------------------------------------
    # Step 1: Strategy Signal Evaluation
    # ------------------------------------------------------------------

    @staticmethod
    def _trade_direction(trade: Dict[str, Any]) -> str:
        """Normalize the three strategy wrappers to bullish/bearish."""
        signal_type = str(trade.get("signal_type") or "").lower()
        option_type = str(trade.get("option_type") or "").upper()
        if signal_type in {"bearish", "breakdown", "short"} or option_type == "PE":
            return "bearish"
        return "bullish"

    @classmethod
    def evaluate_strategy_signals_pure(
        cls,
        candles: List[Dict[str, Any]],
        strategy_name: str,
        underlying_symbol: str = "NIFTY",
        contract_specs: Optional[Dict[str, Any]] = None,
        index: Optional[int] = None,
    ) -> List[OptionsCandidate]:
        """Evaluate a strategy without constructing a stateful pipeline.

        The historical ``evaluate_strategy_signals`` method intentionally
        remains unchanged for the research/backtest workflow.  Production
        intraday planning must not construct :class:`CandidatePipeline`,
        because its constructor resets the experimental risk singleton and
        the old method delegates to backtest wrappers.  This pure entry point
        calls the strategy classes' signal boundary directly and only builds
        the existing ``OptionsCandidate`` value objects.

        ``index`` defaults to the last validated, completed bar.  Callers may
        provide an explicit index when testing a particular completed bar.
        No risk state, P&L, or trade lifecycle is touched here.
        """
        from strategies.orb_strategy import OrbStrategy
        from strategies.trend_pullback_strategy import TrendPullbackStrategy
        from strategies.momentum_continuation_strategy import (
            MomentumContinuationStrategy,
        )

        key = str(strategy_name or "").strip().lower()
        strategy_types = {
            "orb": OrbStrategy,
            "pullback": TrendPullbackStrategy,
            "momentum": MomentumContinuationStrategy,
        }
        strategy_type = strategy_types.get(key)
        if strategy_type is None or not candles:
            return []

        specs = dict(contract_specs or {})
        index = len(candles) - 1 if index is None else int(index)
        if index < 0 or index >= len(candles):
            return []

        strategy = strategy_type(lot_size=int(specs.get("lot_size") or 0))
        signal = strategy.evaluate_signal(
            candles,
            index,
            specs,
            underlying_symbol=underlying_symbol,
        )

        if isinstance(signal, dict):
            signal_dict = signal
        else:
            signal_dict = dict(getattr(signal, "__dict__", {}) or {})
        if not bool(signal_dict.get("allowed")):
            return []

        option_type = str(signal_dict.get("option_type") or "").upper()
        if option_type not in {"CE", "PE"}:
            return []
        candle = candles[index] or {}
        timestamp = signal_dict.get("trigger_time") or signal_dict.get("timestamp")
        timestamp = timestamp or candle.get("timestamp", "")
        entry_price = signal_dict.get("entry_price")
        stop_loss = signal_dict.get("stop_loss")
        target = signal_dict.get("target")
        try:
            underlying_price = float(entry_price)
        except (TypeError, ValueError):
            underlying_price = 0.0

        candidate = OptionsCandidate(
            candidate_id=(
                f"{key.upper()}_{timestamp or 'unknown'}_{index}"
            ),
            signal_timestamp=str(timestamp or ""),
            underlying_symbol=underlying_symbol,
            strategy_name={
                "orb": "ORB",
                "pullback": "Pullback",
                "momentum": "Momentum",
            }[key],
            direction=cls._trade_direction(signal_dict),
            option_type=option_type,
            entry_price=entry_price or 0.0,
            stop_loss=stop_loss or 0.0,
            target=target or 0.0,
            signal_reason=(
                signal_dict.get("reasoning")
                or signal_dict.get("signal_reason")
                or f"{key} signal"
            ),
            underlying_price_at_signal=underlying_price,
            exchange=str(candle.get("exchange") or "NSE").upper(),
        )
        return [candidate]

    @staticmethod
    def rank_candidates_pure(
        candidates: List[OptionsCandidate],
    ) -> List[OptionsCandidate]:
        """Deterministically rank candidates without touching pipeline state."""
        return sorted(
            list(candidates or []),
            key=lambda candidate: (
                0 if candidate.contract_eligibility and
                candidate.contract_eligibility[0] else 1,
                -float(candidate.contract_rank_score or 0.0),
                candidate.candidate_id,
            ),
        )
    
    def evaluate_strategy_signals(
        self,
        candles: List[Dict[str, Any]],
        strategy_name: str,
        underlying_symbol: str = "NIFTY",
    ) -> List[OptionsCandidate]:
        """Evaluate a single strategy and generate candidates.
        
        Args:
            candles: 5-minute candle data for the underlying
            strategy_name: Name of the strategy ("orb", "pullback", "momentum")
            underlying_symbol: Underlying symbol identifier
            
        Returns:
            List of OptionsCandidate objects generated by the strategy
        """
        from strategies.orb_strategy import run_orb_backtest
        from strategies.trend_pullback_strategy import run_pullback_backtest
        from strategies.momentum_continuation_strategy import run_momentum_backtest
        
        candidates = []
        
        if strategy_name == "orb":
            # ORB uses exactly 3 candles covering 9:15-9:30 AM IST
            orb_result = run_orb_backtest(
                self.csv_candles_path,
                self.start_date,
                self.end_date,
                lot_size=self.contract_specs.get("lot_size", 65),
                initial_equity=self.initial_equity,
            )
            
            orb_trades = orb_result.get("trades", [])
            for trade in orb_trades:
                candidate = OptionsCandidate(
                    candidate_id=f"ORB_{trade.get('timestamp', 'unknown')}_{self.next_trade_id}",
                    signal_timestamp=trade.get("timestamp", ""),
                    underlying_symbol=underlying_symbol,
                    strategy_name="ORB",
                    direction=self._trade_direction(trade),
                    option_type=trade.get("option_type", "CE"),
                    entry_price=trade.get("entry_price", 0),
                    stop_loss=trade.get("stop_loss", 0),
                    target=trade.get("target", 0),
                    signal_reason=trade.get("signal_reason", "ORB breakout"),
                    underlying_price_at_signal=trade.get("underlying_price", 23500.0),
                    exchange=trade.get("exchange", "NSE"),
                )
                candidates.append(candidate)
        
        elif strategy_name == "pullback":
            pullback_result = run_pullback_backtest(
                self.csv_candles_path,
                self.start_date,
                self.end_date,
                lot_size=self.contract_specs.get("lot_size", 65),
                initial_equity=self.initial_equity,
            )
            
            pullback_trades = pullback_result.get("trades", [])
            for trade in pullback_trades:
                candidate = OptionsCandidate(
                    candidate_id=f"Pullback_{trade.get('timestamp', 'unknown')}_{self.next_trade_id}",
                    signal_timestamp=trade.get("timestamp", ""),
                    underlying_symbol=underlying_symbol,
                    strategy_name="Pullback",
                    direction=self._trade_direction(trade),
                    option_type=trade.get("option_type", "CE"),
                    entry_price=trade.get("entry_price", 0),
                    stop_loss=trade.get("stop_loss", 0),
                    target=trade.get("target", 0),
                    signal_reason=trade.get("signal_reason", "Pullback entry"),
                    underlying_price_at_signal=trade.get("underlying_price", 23500.0),
                    exchange=trade.get("exchange", "NSE"),
                )
                candidates.append(candidate)
        
        elif strategy_name == "momentum":
            momentum_result = run_momentum_backtest(
                self.csv_candles_path,
                self.start_date,
                self.end_date,
                lot_size=self.contract_specs.get("lot_size", 65),
                initial_equity=self.initial_equity,
            )
            
            momentum_trades = momentum_result.get("trades", [])
            for trade in momentum_trades:
                candidate = OptionsCandidate(
                    candidate_id=f"Momentum_{trade.get('timestamp', 'unknown')}_{self.next_trade_id}",
                    signal_timestamp=trade.get("timestamp", ""),
                    underlying_symbol=underlying_symbol,
                    strategy_name="Momentum",
                    direction=self._trade_direction(trade),
                    option_type=trade.get("option_type", "CE"),
                    entry_price=trade.get("entry_price", 0),
                    stop_loss=trade.get("stop_loss", 0),
                    target=trade.get("target", 0),
                    signal_reason=trade.get("signal_reason", "Momentum continuation"),
                    underlying_price_at_signal=trade.get("underlying_price", 23500.0),
                    exchange=trade.get("exchange", "NSE"),
                )
                candidates.append(candidate)
        
        # Add candidates to pipeline
        self.candidates.extend(candidates)
        
        return candidates
    
    # ------------------------------------------------------------------
    # Step 2: Contract Selection
    # ------------------------------------------------------------------
    
    def select_contracts_for_candidates(
        self,
        available_contracts: Optional[List[Dict[str, Any]]] = None,
    ) -> List[OptionsCandidate]:
        """Run contract selection for all candidates through the contract selector.
        
        For each candidate, filters available contracts and selects the best one
        by moneyness using the contract selector.
        
        Returns:
            List of candidates with contract selection results
        """
        records = list(available_contracts or self.available_option_records)
        for candidate in self.candidates:
            expected_option_type = "CE" if candidate.direction.lower() == "bullish" else "PE"
            if candidate.option_type.upper() != expected_option_type:
                reason = (
                    f"Option direction mismatch: {candidate.direction} requires "
                    f"{expected_option_type}, got {candidate.option_type}"
                )
                candidate.contract_eligibility = (False, reason)
                candidate.contract_rank_score = 0.0
                self.rejected_candidates.append({
                    "candidate": candidate,
                    "reason": reason,
                    "stage": "contract_selection",
                })
                continue
            # An injected candidate may carry its own fixture/history window;
            # otherwise use the pipeline's supplied option records.
            candidate_records = list(candidate.option_candles or records)
            specs = get_contract_specifications(candidate.underlying_symbol)
            min_oi = specs.get("min_oi", 0)
            result = select_option_contract(
                underlying_symbol=candidate.underlying_symbol,
                underlying_price=candidate.underlying_price_at_signal,
                signal_direction=candidate.direction,
                available_contracts=candidate_records,
                contract_specs=specs,
                min_oi=min_oi,
                max_spread_pct=5.0,
                moneyness_target="ATM",
                expected_exchange=candidate.exchange,
            )

            if "selected_contract" in result:
                selected = dict(result["selected_contract"])
                candidate.selected_contract = selected
                candidate.contract_eligibility = (True, "Eligible contract selected")
                candidate.contract_rank_score = self._contract_rank_score(selected)
                candidate.contract_lot_size = int(
                    selected.get("lot_size") or specs.get("lot_size") or 0
                )
                candidate.entry_bid = selected.get("bid")
                candidate.entry_ask = selected.get("ask")
                # Long option entries use ask when available.  The fallback to
                # close is only for a validated candle that lacks a quote and
                # is not used by the execution path when bid/ask are required.
                candidate.option_entry_premium = selected.get("ask")
            else:
                reason = result.get("rejection", {}).get(
                    "reason", "No eligible contract found"
                )
                candidate.contract_eligibility = (False, reason)
                candidate.contract_rank_score = 0.0
                self.rejected_candidates.append({
                    "candidate": candidate,
                    "reason": reason,
                    "stage": "contract_selection",
                })
        
        return self.candidates

    @staticmethod
    def _contract_rank_score(contract: Dict[str, Any]) -> float:
        """Deterministic liquidity-aware score for already eligible contracts."""
        bid = float(contract.get("bid") or 0)
        ask = float(contract.get("ask") or 0)
        mid = (bid + ask) / 2 if bid and ask else 0
        spread_pct = ((ask - bid) / mid * 100) if mid else 100.0
        oi = float(contract.get("open_interest") or 0)
        return round(100.0 - spread_pct + min(oi / 10000.0, 10.0), 4)
    
    # ------------------------------------------------------------------
    # Step 3: Candidate Ranking
    # ------------------------------------------------------------------
    
    def rank_candidates(self) -> List[OptionsCandidate]:
        """Rank all candidates by eligibility and score.
        
        Ranking priorities (in order):
        1. Eligible contracts (rank > 0)
        2. Higher contract rank score first
        3. Bullish CE signals ranked before bearish PE when scores tied
        4. Deterministic tie-breaking by candidate_id
        
        Returns:
            Candidates sorted by rank (best first)
        """
        def rank_score(candidate: OptionsCandidate) -> Tuple[int, float, str]:
            """Return a ranking key: (eligibility_priority, score, tiebreaker)."""
            # Eligibility priority: eligible first (0), then rejected (1)
            eligible_priority = 0 if candidate.contract_eligibility and candidate.contract_eligibility[0] else 1
            
            # Score: higher is better
            score = candidate.contract_rank_score
            
            # Tiebreaker: lower candidate_id comes first deterministically
            tiebreaker = candidate.candidate_id
            
            return (eligible_priority, -score, tiebreaker)
        
        # Sort candidates by rank
        ranked = sorted(self.candidates, key=rank_score)
        
        return ranked
    
    # ------------------------------------------------------------------
    # Step 4: Portfolio Risk Approval
    # ------------------------------------------------------------------
    
    def approve_candidates_with_risk(
        self,
        ranked_candidates: List[OptionsCandidate],
    ) -> Tuple[List[OptionsCandidate], List[Dict[str, Any]]]:
        """Run ranked candidates through portfolio risk manager.
        
        For each candidate, applies all experimental risk controls:
        - Daily loss limit
        - Max trades per day
        - Consecutive losses guard
        - Time cutoff (IST)
        - Duplicate entry prevention
        - Position sizing
        
        Args:
            ranked_candidates: Candidates sorted by rank (best first)
            
        Returns:
            Tuple of (approved candidates, rejected candidates with reasons)
        """
        approved = []
        rejected = []
        
        for candidate in ranked_candidates:
            # A valid underlying signal is not enough.  Contract selection is
            # a mandatory gate before any portfolio-risk approval.
            if not candidate.contract_eligibility or not candidate.contract_eligibility[0]:
                reason = (
                    candidate.contract_eligibility[1]
                    if candidate.contract_eligibility
                    else "Contract selection has not been completed"
                )
                candidate.risk_approved = False
                candidate.risk_rejection_reason = f"CONTRACT: {reason}"
                rejection = {
                    "candidate": candidate,
                    "reason": candidate.risk_rejection_reason,
                    "stage": "contract_selection",
                }
                rejected.append(rejection)
                self.rejected_candidates.append(rejection)
                continue

            if not candidate.selected_contract:
                reason = "No selected contract"
                candidate.risk_approved = False
                candidate.risk_rejection_reason = f"CONTRACT: {reason}"
                rejection = {"candidate": candidate, "reason": candidate.risk_rejection_reason, "stage": "contract_selection"}
                rejected.append(rejection)
                self.rejected_candidates.append(rejection)
                continue
            
            # 1. Check daily loss limit
            allowed, reason = check_daily_loss_limit(
                pnl=candidate.contract_rank_score * 0,  # P&L will be realized after trade
                starting_equity=self.initial_equity,
                loss_limit_pct=self.max_daily_loss_pct,
            )
            
            if not allowed:
                candidate.risk_approved = False
                candidate.risk_rejection_reason = f"DAILY LOSS LIMIT: {reason}"
                rejected.append({
                    "candidate": candidate,
                    "reason": f"DAILY LOSS LIMIT: {reason}",
                    "stage": "daily_loss_limit",
                })
                continue
            
            # 2. Check max trades per day
            allowed, reason = check_max_trades_per_day(
                trade_count_today=self.daily_trade_count,
                max_trades=self.max_trades_per_day,
            )
            
            if not allowed:
                candidate.risk_approved = False
                candidate.risk_rejection_reason = f"MAX TRADES PER DAY: {reason}"
                rejected.append({
                    "candidate": candidate,
                    "reason": f"MAX TRADES PER DAY: {reason}",
                    "stage": "max_trades_per_day",
                })
                continue
            
            # 3. Check consecutive losses without recording a new outcome.
            state = get_experimental_risk_state()
            allowed, reason = check_consecutive_losses(
                pnl=0,
                max_consecutive=self.max_consecutive_losses,
                record_outcome=False,
            )
            
            if not allowed:
                candidate.risk_approved = False
                candidate.risk_rejection_reason = f"CONSECUTIVE LOSSES: {reason}"
                rejected.append({
                    "candidate": candidate,
                    "reason": f"CONSECUTIVE LOSSES: {reason}",
                    "stage": "consecutive_losses",
                })
                continue
            
            # 4. Check time cutoff (IST), failing closed for malformed input.
            allowed, reason = check_time_cutoff_ist(
                trade_time_ist=candidate.signal_timestamp
            )
            
            if not allowed:
                candidate.risk_approved = False
                candidate.risk_rejection_reason = f"TIME CUTOFF: {reason}"
                rejected.append({
                    "candidate": candidate,
                    "reason": f"TIME CUTOFF: {reason}",
                    "stage": "time_cutoff",
                })
                continue
            
            # 5. Check aggregate open positions and correlated exposure.
            active_positions = dict(state.positions if state else {})
            active_positions.update({item.underlying_symbol: {"candidate_id": item.candidate_id} for item in approved})
            active_count = max(
                int(state.open_positions) if state else 0,
                len(active_positions),
            ) + len(approved)
            if active_count >= self.max_open_positions:
                allowed, reason = False, (
                    f"MAX OPEN POSITIONS BREACH: {active_count} active, "
                    f"limit is {self.max_open_positions}"
                )
            else:
                allowed, reason = True, ""
            if not allowed:
                candidate.risk_approved = False
                candidate.risk_rejection_reason = f"MAX OPEN POSITIONS: {reason}"
                rejection = {"candidate": candidate, "reason": candidate.risk_rejection_reason, "stage": "max_open_positions"}
                rejected.append(rejection)
                self.rejected_candidates.append(rejection)
                continue

            allowed, reason = check_correlated_exposure(candidate.underlying_symbol)
            if allowed:
                candidate_symbols = {item.underlying_symbol.upper() for item in approved}
                for group in self.correlated_groups:
                    group_upper = {str(item).upper() for item in group}
                    if candidate.underlying_symbol.upper() in group_upper and candidate_symbols.intersection(group_upper):
                        allowed = False
                        reason = f"CORRELATED EXPOSURE: conflicts with {sorted(candidate_symbols.intersection(group_upper))}"
                        break
            if not allowed:
                candidate.risk_approved = False
                candidate.risk_rejection_reason = f"CORRELATED EXPOSURE: {reason}"
                rejection = {"candidate": candidate, "reason": candidate.risk_rejection_reason, "stage": "correlated_exposure"}
                rejected.append(rejection)
                self.rejected_candidates.append(rejection)
                continue

            # 6. Check for duplicate entries using the active state.
            allowed, reason = check_duplicate_entry(
                symbol=candidate.underlying_symbol,
                existing_positions=active_positions,
            )
            
            if not allowed:
                candidate.risk_approved = False
                candidate.risk_rejection_reason = f"DUPLICATE ENTRY: {reason}"
                rejected.append({
                    "candidate": candidate,
                    "reason": f"DUPLICATE ENTRY: {reason}",
                    "stage": "duplicate_entry",
                })
                continue
            
            # 7. Size the long option from the selected premium and verified
            # lot size.  Underlying points are never used as option P&L.
            entry_premium = candidate.option_entry_premium
            lot_size = candidate.contract_lot_size
            if not entry_premium or entry_premium <= 0 or lot_size <= 0:
                reason = "Missing valid option entry premium or lot size"
                candidate.risk_approved = False
                candidate.risk_rejection_reason = f"POSITION SIZE: {reason}"
                rejection = {"candidate": candidate, "reason": candidate.risk_rejection_reason, "stage": "position_sizing"}
                rejected.append(rejection)
                self.rejected_candidates.append(rejection)
                continue
            risk_amount = self.initial_equity * self.risk_per_trade_pct / 100.0
            affordable_units = int(risk_amount / float(entry_premium))
            position_size = (affordable_units // lot_size) * lot_size
            if position_size <= 0:
                reason = "Risk budget cannot fund one complete option lot"
                candidate.risk_approved = False
                candidate.risk_rejection_reason = f"POSITION SIZE: {reason}"
                rejection = {"candidate": candidate, "reason": candidate.risk_rejection_reason, "stage": "position_sizing"}
                rejected.append(rejection)
                self.rejected_candidates.append(rejection)
                continue
            
            # 7. Build approved candidate
            candidate.risk_approved = True
            candidate.position_size = position_size
            candidate.daily_pnl_impact = 0.0  # Will be realized after trade
            candidate.contract_eligibility = (
                True,
                f"Approved: {position_size} option units at premium {entry_premium}",
            )
            
            approved.append(candidate)
        
        return approved, rejected
    
    # ------------------------------------------------------------------
    # Step 5: Trade Lifecycle Execution
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_timestamp(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        try:
            text = str(value).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=IST)
            return parsed.astimezone(IST)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _same_contract(row: Dict[str, Any], selected: Dict[str, Any]) -> bool:
        return (
            str(row.get("underlying_symbol", "")).upper()
            == str(selected.get("underlying_symbol", "")).upper()
            and str(row.get("option_type", "")).upper()
            == str(selected.get("option_type", "")).upper()
            and float(row.get("strike", 0) or 0) == float(selected.get("strike", 0) or 0)
            and str(row.get("expiry", "")) == str(selected.get("expiry", ""))
        )

    def _resolve_exit(
        self,
        candidate: OptionsCandidate,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Resolve an underlying-triggered exit to a real option quote."""
        selected = candidate.selected_contract or {}
        entry_time = self._parse_timestamp(candidate.signal_timestamp)
        if entry_time is None:
            return None, "invalid_entry_timestamp"

        option_rows = [
            row for row in (candidate.option_candles or [])
            if self._same_contract(row, selected)
            and self._parse_timestamp(row.get("timestamp"))
            and self._parse_timestamp(row.get("timestamp")) > entry_time
        ]
        option_rows.sort(key=lambda row: self._parse_timestamp(row.get("timestamp")))
        by_time = {
            self._parse_timestamp(row.get("timestamp")): row for row in option_rows
        }

        underlying_rows = [
            row for row in (candidate.underlying_candles or [])
            if self._parse_timestamp(row.get("timestamp"))
            and self._parse_timestamp(row.get("timestamp")) > entry_time
        ]
        underlying_rows.sort(key=lambda row: self._parse_timestamp(row.get("timestamp")))

        # Strategy levels are underlying levels.  If no later underlying data
        # exists, no trigger may be inferred from the option premium itself.
        for underlying in underlying_rows:
            ts = self._parse_timestamp(underlying.get("timestamp"))
            if ts is None:
                continue
            high = float(underlying.get("high", 0) or 0)
            low = float(underlying.get("low", 0) or 0)
            if candidate.direction == "bullish":
                stop_hit = low <= candidate.stop_loss
                target_hit = high >= candidate.target
            else:
                stop_hit = high >= candidate.stop_loss
                target_hit = low <= candidate.target
            if not stop_hit and not target_hit:
                continue

            # Conservative policy for an OHLC candle that hits both levels:
            # stop is assumed first because ordering is unknowable.
            reason = "stop_loss" if stop_hit else "target"
            option_row = by_time.get(ts)
            if option_row is None:
                return None, "missing_exit_premium"
            if option_row.get("bid") is None or float(option_row.get("bid") or 0) <= 0:
                return None, "missing_exit_bid"
            return option_row, reason

        # If no intraday trigger occurred, close at the last later aligned
        # quote.  This is a simulated end-of-day closure, not a fabricated
        # price.
        if option_rows:
            return option_rows[-1], "end_of_day"
        return None, "missing_exit_premium"

    def execute_approved_trades(self, approved_candidates: List[OptionsCandidate]) -> Dict[str, Any]:
        """Execute approved candidates as simulated trades with P&L calculation.
        
        For each approved candidate, records the trade with full details and
        updates daily P&L, trade count, and consecutive loss tracking.
        
        Args:
            approved_candidates: Candidates that passed risk approval
            
        Returns:
            Dict with trade execution results and performance metrics
        """
        executed_trades = []
        
        for candidate in approved_candidates:
            selected = candidate.selected_contract or {}
            entry_ask = candidate.entry_ask or selected.get("ask")
            if not entry_ask or float(entry_ask) <= 0:
                candidate.risk_rejection_reason = "Missing valid option ask for entry"
                continue
            option_row, exit_reason = self._resolve_exit(candidate)
            if option_row is None:
                candidate.risk_rejection_reason = f"No valid option exit: {exit_reason}"
                self.rejected_candidates.append({
                    "candidate": candidate,
                    "reason": candidate.risk_rejection_reason,
                    "stage": "trade_execution",
                })
                continue

            exit_bid = option_row.get("bid")
            if not exit_bid or float(exit_bid) <= 0:
                candidate.risk_rejection_reason = "Missing valid option bid for exit"
                continue

            lot_size = candidate.contract_lot_size
            if lot_size <= 0 or candidate.position_size <= 0:
                candidate.risk_rejection_reason = "Missing valid lot size or position size"
                continue

            # Premium accounting is deliberately independent of the
            # underlying trigger levels.  The quote sides define the fill.
            entry_premium = float(entry_ask)
            exit_premium = float(exit_bid)
            units = int(candidate.position_size)
            gross_pnl = (exit_premium - entry_premium) * units
            tick_value = 0.05
            slippage_per_leg = float(selected.get("slippage_ticks", 0.5)) * tick_value
            slippage_cost = slippage_per_leg * 2 * units
            brokerage_per_order = float(selected.get("brokerage_per_order", 20.0))
            transaction_costs = brokerage_per_order * 2 + slippage_cost
            net_pnl = gross_pnl - transaction_costs

            candidate.trade_executed = True
            trade_id = f"T{self.next_trade_id:04d}"
            candidate.trade_id = trade_id
            self.next_trade_id += 1
            candidate.option_entry_premium = entry_premium
            candidate.option_exit_premium = exit_premium
            candidate.exit_timestamp = option_row.get("timestamp")
            candidate.exit_reason = exit_reason
            candidate.gross_pnl = round(gross_pnl, 2)
            candidate.transaction_costs = round(transaction_costs, 2)
            candidate.net_pnl = round(net_pnl, 2)
            candidate.realized_pnl = candidate.net_pnl

            trade_record = {
                "trade_id": trade_id,
                "candidate_id": candidate.candidate_id,
                "strategy": candidate.strategy_name,
                "underlying_symbol": candidate.underlying_symbol,
                "exchange": candidate.exchange,
                "option_type": candidate.option_type,
                "strike": selected.get("strike"),
                "expiry": selected.get("expiry"),
                "lot_size": lot_size,
                "underlying_entry_price": candidate.entry_price,
                "underlying_stop_loss": candidate.stop_loss,
                "underlying_target": candidate.target,
                "option_entry_premium": entry_premium,
                "option_exit_premium": exit_premium,
                "position_size": units,
                "trigger_time": candidate.signal_timestamp,
                "exit_timestamp": candidate.exit_timestamp,
                "exit_reason": exit_reason,
                "gross_pnl": candidate.gross_pnl,
                "transaction_costs": candidate.transaction_costs,
                "realized_pnl": candidate.realized_pnl,
                "daily_pnl_after": round(self.daily_pnl + net_pnl, 2),
                "risk_per_trade_pct": self.risk_per_trade_pct,
                "signal_reason": candidate.signal_reason,
                "contract_selected": True,
                "research_only": True,
            }

            executed_trades.append(trade_record)
            self.daily_trade_count += 1
            self.daily_pnl += net_pnl
            if net_pnl < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0
            if self.daily_pnl > self.high_watermark:
                self.high_watermark = self.daily_pnl
            record_experimental_trade_outcome(candidate.underlying_symbol, net_pnl)

            daily_loss_limit = self.initial_equity * self.max_daily_loss_pct / 100
            if self.daily_pnl < -daily_loss_limit:
                break
        
        # Store approved trades
        self.approved_trades.extend(executed_trades)
        
        return {
            "executed_trades": executed_trades,
            "total_trades": len(executed_trades),
            "total_pnl": round(self.daily_pnl, 2),
            "win_rate": 0,  # Will be calculated after analysis
            "consecutive_losses": self.consecutive_losses,
            "high_watermark": round(self.high_watermark, 2),
            "daily_trade_count": self.daily_trade_count,
        }
    
    # ------------------------------------------------------------------
    # Step 6: Full Pipeline Execution
    # ------------------------------------------------------------------
    
    def run_full_pipeline(
        self,
        strategy_names: List[str] = None,
    ) -> Dict[str, Any]:
        """Run the complete candidate-to-trade pipeline.
        
        Executes all steps:
        1. Import data
        2. Evaluate strategy signals
        3. Select contracts
        4. Rank candidates
        5. Approve with risk controls
        6. Execute trades
        
        Args:
            strategy_names: List of strategy names to evaluate (None = all)
            
        Returns:
            Complete pipeline results
        """
        # Step 0: Reset pipeline state
        self.reset_pipeline()
        
        # Step 1: Import data
        print("Step 1: Importing market data...")
        candles = import_5min_index_candles(
            self.csv_candles_path,
            underlying_symbol="NIFTY",
            start_date=self.start_date,
            end_date=self.end_date,
        )
        print(f"  Imported {len(candles)} 5-minute candles\n")
        
        # Step 2: Evaluate strategy signals
        print("Step 2: Evaluating strategy signals...")
        if strategy_names is None:
            strategy_names = ["orb", "pullback", "momentum"]
        
        all_candidates = []
        for sname in strategy_names:
            print(f"  Evaluating {sname}...")
            candidates = self.evaluate_strategy_signals(
                candles=candles,
                strategy_name=sname,
                underlying_symbol="NIFTY",
            )
            all_candidates.extend(candidates)
            print(f"    {len(candidates)} candidates generated")
        print()
        
        # Step 3: Contract selection
        print("Step 3: Contract selection...")
        candidates_with_contracts = self.select_contracts_for_candidates()
        eligible_count = sum(
            1 for c in candidates_with_contracts 
            if c.contract_eligibility and c.contract_eligibility[0]
        )
        print(f"  {eligible_count}/{len(candidates_with_contracts)} candidates have eligible contracts\n")
        
        # Step 4: Candidate ranking
        print("Step 4: Candidate ranking...")
        ranked_candidates = self.rank_candidates()
        print(f"  {len(ranked_candidates)} candidates ranked\n")
        
        # Step 5: Portfolio risk approval
        print("Step 5: Portfolio risk approval...")
        approved, rejected = self.approve_candidates_with_risk(ranked_candidates)
        print(f"  {len(approved)} approved, {len(rejected)} rejected\n")
        
        # Step 6: Execute trades
        print("Step 6: Execute trades...")
        execution_results = self.execute_approved_trades(approved)
        print(f"  {execution_results['total_trades']} trades executed\n")
        
        # Return comprehensive results
        return {
            "candle_count": len(candles),
            "strategy_names": strategy_names,
            "total_candidates_generated": len(all_candidates),
            "candidates_with_contract_eligibility": eligible_count,
            "ranked_candidates": len(ranked_candidates),
            "risk_approved": len(approved),
            "risk_rejected": len(rejected),
            "executed_trades": execution_results["total_trades"],
            "total_pnl": execution_results["total_pnl"],
            "executed_trade_details": execution_results["executed_trades"],
            "approved_trades": self.approved_trades,
            "rejected_candidates": [
                r for r in rejected  # type: ignore
                if isinstance(r, dict) and "reason" in r
            ],
            "risk_settings": {
                "risk_per_trade_pct": self.risk_per_trade_pct,
                "max_daily_loss_pct": self.max_daily_loss_pct,
                "max_trades_per_day": self.max_trades_per_day,
                "max_consecutive_losses": self.max_consecutive_losses,
                "time_cutoff_ist": self.time_cutoff_ist,
            },
            "options_pnl_readiness": options_pnl_readiness(
                self.available_option_records,
                provenance=self.option_data_provenance,
            ),
            "research_only": True,
        }


# ---------------------------------------------------------------------------
# QUICK TEST FUNCTION
# ---------------------------------------------------------------------------

def test_pipeline(csv_path: str = None) -> Dict[str, Any]:
    """Test the complete candidate pipeline.
    
    Args:
        csv_path: Path to 5-min NIFTY CSV (uses sample data if None)
        
    Returns:
        Dict with pipeline test results
    """
    print("=" * 60)
    print("TRIO CANDIDATE PIPELINE TEST")
    print("=" * 60)
    
    # Initialize pipeline
    pipeline = CandidatePipeline(
        csv_candles_path=csv_path or "",
        initial_equity=500000.0,
        risk_per_trade_pct=0.30,
        max_daily_loss_pct=1.0,
        max_trades_per_day=5,
        max_consecutive_losses=2,
        time_cutoff_ist="14:30",
    )
    
    # Run full pipeline
    results = pipeline.run_full_pipeline(strategy_names=["orb", "pullback", "momentum"])
    
    print()
    print("=" * 60)
    print("PIPELINE RESULTS SUMMARY")
    print("=" * 60)
    print(f"  Candles imported: {results['candle_count']}")
    print(f"  Strategies evaluated: {results['strategy_names']}")
    print(f"  Total candidates: {results['total_candidates_generated']}")
    print(f"  Candidates with eligible contracts: {results['candidates_with_contract_eligibility']}")
    print(f"  Ranked candidates: {results['ranked_candidates']}")
    print(f"  Risk-approved: {results['risk_approved']}")
    print(f"  Risk-rejected: {results['risk_rejected']}")
    print(f"  Trades executed: {results['executed_trades']}")
    print(f"  Total P&L: {results['total_pnl']}")
    print()
    print("Risk Settings:")
    for k, v in results["risk_settings"].items():
        print(f"  {k}: {v}")
    print()
    
    # Save detailed results
    import json
    with open("output/pipeline_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"  Results saved to: output/pipeline_results.json\n")
    
    return results
