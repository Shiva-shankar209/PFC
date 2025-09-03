from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

APP_SAVE = os.path.join(os.path.expanduser("~"), ".pf_chatbot_profile.json")

# ----------------------------- Utilities -----------------------------

def currency(n: float) -> str:
    """Format number as Indian currency with lakh/crore separators."""
    # Simple formatter (not locale-perfect but readable)
    neg = n < 0
    n = abs(n)
    s = f"{n:,.2f}"
    # Convert to Indian grouping (##,##,###)
    parts = s.split('.')
    whole = parts[0].replace(',', '')
    if len(whole) > 3:
        head = whole[:-3]
        tail = whole[-3:]
        # insert commas every 2 digits in head
        head_groups = []
        while head:
            head_groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ','.join(head_groups + [tail])
    else:
        whole = whole
    res = whole + '.' + parts[1]
    return ("-₹" if neg else "₹") + res


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

# ----------------------------- Persistence -----------------------------

@dataclass
class UserProfile:
    name: str = "Friend"
    age: Optional[int] = None
    monthly_income: Optional[float] = None
    monthly_expenses: Optional[float] = None
    emergency_months: int = 6
    risk: str = "moderate"  # conservative / moderate / aggressive
    city: Optional[str] = None
    regime_preference: Optional[str] = None  # "new" or "old"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def savings_capacity(self) -> Optional[float]:
        if self.monthly_income is None or self.monthly_expenses is None:
            return None
        return max(0.0, self.monthly_income - self.monthly_expenses)


def load_profile(path: str = APP_SAVE) -> UserProfile:
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return UserProfile(**data)
        except Exception:
            pass
    return UserProfile()


def save_profile(profile: UserProfile, path: str = APP_SAVE) -> None:
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(asdict(profile), f, indent=2)
    except Exception as e:
        print(f"[warn] Could not save profile: {e}")

# ----------------------------- Tax Engine (India) -----------------------------

@dataclass
class TaxSlab:
    up_to: Optional[float]  # upper bound for slab (None means no upper bound)
    rate: float             # e.g., 0.05 for 5%

@dataclass
class TaxConfig:
    # FY 2024-25 (Assessment Year 2025-26) baseline; update as needed
    # Rebate u/s 87A thresholds
    rebate_threshold_new: float = 700000.0
    rebate_threshold_old: float = 500000.0

    # Standard deduction (salary/pension)
    std_deduction_new: float = 50000.0
    std_deduction_old: float = 50000.0

    # Common deductions (examples)
    max_80C_old: float = 150000.0  # PPF/EPF/ELSS/Principal etc. (Old only)
    max_80D_old: float = 25000.0   # Health insurance premium (Old only; simpl.)

    cess_rate: float = 0.04

    # New regime slabs
    slabs_new: List[TaxSlab] = field(default_factory=lambda: [
        TaxSlab(300000, 0.00),
        TaxSlab(600000, 0.05),
        TaxSlab(900000, 0.10),
        TaxSlab(1200000, 0.15),
        TaxSlab(1500000, 0.20),
        TaxSlab(None, 0.30),
    ])

    # Old regime slabs
    slabs_old: List[TaxSlab] = field(default_factory=lambda: [
        TaxSlab(250000, 0.00),
        TaxSlab(500000, 0.05),
        TaxSlab(1000000, 0.20),
        TaxSlab(None, 0.30),
    ])

class IndiaTaxCalculator:
    def __init__(self, cfg: TaxConfig | None = None):
        self.cfg = cfg or TaxConfig()

    def _slab_tax(self, taxable: float, slabs: List[TaxSlab]) -> float:
        tax = 0.0
        lower = 0.0
        for slab in slabs:
            upper = slab.up_to if slab.up_to is not None else taxable
            if taxable > lower:
                segment = min(taxable, upper) - lower
                if segment > 0:
                    tax += segment * slab.rate
            lower = upper
            if taxable <= lower:
                break
        return tax

    def estimate(self,
                 gross_annual_income: float,
                 is_salaried: bool = True,
                 regime: str = "new",
                 deductions_old_80C: float = 0.0,
                 deductions_old_80D: float = 0.0) -> Dict[str, float]:
        """Return detailed tax estimate for India."""
        cfg = self.cfg
        regime = regime.lower().strip()
        if regime not in ("new", "old"):
            regime = "new"

        std_deduction = cfg.std_deduction_new if (is_salaried and regime == "new") else (
                        cfg.std_deduction_old if (is_salaried and regime == "old") else 0.0)

        if regime == "new":
            taxable = max(0.0, gross_annual_income - std_deduction)
            base_tax = self._slab_tax(taxable, cfg.slabs_new)
            # Rebate u/s 87A (new)
            rebate = base_tax if taxable <= cfg.rebate_threshold_new else 0.0
            tax_after_rebate = max(0.0, base_tax - rebate)
            cess = tax_after_rebate * cfg.cess_rate
            total = tax_after_rebate + cess
            return {
                "regime": 1.0,  # 1=new, 0=old for convenience
                "gross": gross_annual_income,
                "std_deduction": std_deduction,
                "deductions": 0.0,
                "taxable": taxable,
                "base_tax": base_tax,
                "rebate": rebate,
                "cess": cess,
                "total_tax": total,
                "effective_rate": total / gross_annual_income if gross_annual_income else 0.0,
            }
        else:  # old regime
            d80c = min(cfg.max_80C_old, max(0.0, deductions_old_80C))
            d80d = min(cfg.max_80D_old, max(0.0, deductions_old_80D))
            deductions = d80c + d80d + (std_deduction if is_salaried else 0.0)
            taxable = max(0.0, gross_annual_income - deductions)
            base_tax = self._slab_tax(taxable, cfg.slabs_old)
            rebate = base_tax if taxable <= cfg.rebate_threshold_old else 0.0
            tax_after_rebate = max(0.0, base_tax - rebate)
            cess = tax_after_rebate * cfg.cess_rate
            total = tax_after_rebate + cess
            return {
                "regime": 0.0,
                "gross": gross_annual_income,
                "std_deduction": std_deduction if is_salaried else 0.0,
                "deductions": deductions - (std_deduction if is_salaried else 0.0),
                "taxable": taxable,
                "base_tax": base_tax,
                "rebate": rebate,
                "cess": cess,
                "total_tax": total,
                "effective_rate": total / gross_annual_income if gross_annual_income else 0.0,
            }

# ----------------------------- Planning Calculators -----------------------------

def future_value_sip(monthly: float, annual_return_pct: float, years: float) -> float:
    r = annual_return_pct / 100.0 / 12.0
    n = int(round(years * 12))
    if r == 0:
        return monthly * n
    return monthly * ((1 + r) ** n - 1) / r * (1 + r)


def required_sip(target: float, annual_return_pct: float, years: float) -> float:
    r = annual_return_pct / 100.0 / 12.0
    n = int(round(years * 12))
    if r == 0:
        return target / n
    return target / (((1 + r) ** n - 1) / r * (1 + r))


def emi(principal: float, annual_rate_pct: float, years: float) -> float:
    r = annual_rate_pct / 100.0 / 12.0
    n = int(round(years * 12))
    if r == 0:
        return principal / n
    return principal * r * (1 + r) ** n / ((1 + r) ** n - 1)


def retirement_target(monthly_expense_today: float, years_to_retire: int, retired_years: int,
                      inflation_pct: float = 6.0, swr_pct: float = 3.5) -> Tuple[float, float]:
    """
    Estimate retirement corpus using a Safe Withdrawal Rate (SWR) model.
    Returns (required_corpus_at_retirement, inflated_monthly_expense_at_retirement)
    """
    inf = inflation_pct / 100.0
    future_monthly = monthly_expense_today * ((1 + inf) ** years_to_retire)
    annual_need = future_monthly * 12
    corpus = annual_need * 100 / swr_pct  # e.g., 3.5% SWR -> * 28.57
    return corpus, future_monthly


def emergency_fund(monthly_expense: float, months: int = 6) -> float:
    return monthly_expense * months

# ----------------------------- Portfolio Suggestion -----------------------------

RISK_ALLOCATION = {
    "conservative": {"equity": 0.3, "debt": 0.6, "gold": 0.1},
    "moderate":     {"equity": 0.6, "debt": 0.3, "gold": 0.1},
    "aggressive":   {"equity": 0.8, "debt": 0.15, "gold": 0.05},
}


def suggest_allocation(risk: str, horizon_years: int) -> Dict[str, float]:
    risk = risk.lower().strip()
    base = RISK_ALLOCATION.get(risk, RISK_ALLOCATION["moderate"]).copy()
    # Nudge equity up for long horizons, down for short
    if horizon_years >= 10:
        base["equity"] = clamp(base["equity"] + 0.05, 0.2, 0.9)
        base["debt"] = clamp(1.0 - base["equity"] - base["gold"], 0.05, 0.75)
    elif horizon_years <= 3:
        base["equity"] = clamp(base["equity"] - 0.15, 0.1, 0.7)
        base["debt"] = clamp(1.0 - base["equity"] - base["gold"], 0.2, 0.85)
    # Normalize
    s = sum(base.values())
    for k in base:
        base[k] = round(base[k] / s, 2)
    return base

# ----------------------------- Chat UX -----------------------------

HELP_TEXT = (
    """
What I can do:
 1) tax           -> Estimate income tax (India, Old vs New)
 2) sip           -> SIP calculator (future value or required monthly)
 3) goal          -> Plan a goal (target amount, return, years)
 4) retirement    -> Retirement corpus estimate
 5) emergency     -> Emergency fund target
 6) emi           -> Loan EMI calculator
 7) allocate      -> Suggested asset allocation
 8) profile       -> View or update your profile
 9) help          -> Show this help
10) quit/exit     -> Save & exit

Tip: You can also just type things like \"tax for 18 lakh new regime\" or
\"need ₹50L in 15 yrs at 12% – how much SIP?\" and I'll try to parse it.
"""
).strip()


class Chatbot:
    def __init__(self):
        self.profile = load_profile()
        self.tax_calc = IndiaTaxCalculator()

    # ------------------- Command Handlers -------------------
    def cmd_help(self, _: List[str]):
        print(HELP_TEXT)

    def cmd_profile(self, args: List[str]):
        p = self.profile
        print("\nCurrent profile:")
        print(json.dumps(asdict(p), indent=2))
        print("\nUpdate fields (press Enter to keep current):")
        try:
            name = input(f"Name [{p.name}]: ").strip() or p.name
            age = input(f"Age [{p.age or ''}]: ").strip()
            income = input(f"Monthly income ₹ [{p.monthly_income or ''}]: ").strip()
            expenses = input(f"Monthly expenses ₹ [{p.monthly_expenses or ''}]: ").strip()
            emergency_months = input(f"Emergency months [{p.emergency_months}]: ").strip()
            risk = input(f"Risk (conservative/moderate/aggressive) [{p.risk}]: ").strip() or p.risk
            city = input(f"City [{p.city or ''}]: ").strip() or p.city
            regime = input(f"Tax regime preference (new/old) [{p.regime_preference or ''}]: ").strip() or p.regime_preference

            p.name = name
            p.age = int(age) if age else p.age
            p.monthly_income = float(income) if income else p.monthly_income
            p.monthly_expenses = float(expenses) if expenses else p.monthly_expenses
            p.emergency_months = int(emergency_months) if emergency_months else p.emergency_months
            p.risk = risk.lower()
            p.city = city
            p.regime_preference = regime.lower() if regime else p.regime_preference
            save_profile(p)
            print("\nSaved ✔\n")
        except KeyboardInterrupt:
            print("\nUpdate cancelled.")
        except Exception as e:
            print(f"[error] {e}")

    def cmd_emergency(self, args: List[str]):
        p = self.profile
        if p.monthly_expenses is None:
            try:
                mexp = float(input("Monthly expenses ₹: "))
            except Exception:
                print("Please enter a number.")
                return
        else:
            mexp = p.monthly_expenses
        months = p.emergency_months
        try:
            s = input(f"Months of buffer [{months}]: ").strip()
            if s:
                months = int(s)
        except Exception:
            pass
        target = emergency_fund(mexp, months)
        print(f"Emergency fund target for {months} months: {currency(target)}")

    def cmd_sip(self, args: List[str]):
        print("SIP calculator → Choose mode:")
        print(" 1) Future value (given monthly SIP)")
        print(" 2) Required SIP (given target)")
        choice = input("Enter 1 or 2: ").strip()
        try:
            if choice == '1':
                monthly = float(input("Monthly SIP ₹: "))
                rate = float(input("Expected annual return %: "))
                years = float(input("Years: "))
                fv = future_value_sip(monthly, rate, years)
                print(f"Future value ≈ {currency(fv)}")
            elif choice == '2':
                target = float(input("Target amount ₹: "))
                rate = float(input("Expected annual return %: "))
                years = float(input("Years: "))
                sip_amt = required_sip(target, rate, years)
                print(f"Required monthly SIP ≈ {currency(sip_amt)}")
            else:
                print("Invalid choice.")
        except Exception:
            print("Please enter valid numbers.")

    def cmd_goal(self, args: List[str]):
        try:
            target = float(input("Target amount ₹: "))
            years = float(input("Years to goal: "))
            rate = float(input("Expected annual return %: "))
            sip_amt = required_sip(target, rate, years)
            print(f"To reach {currency(target)} in {years:.1f} years at {rate:.2f}% p.a., invest ≈ {currency(sip_amt)} per month.")
        except Exception:
            print("Please enter valid numbers.")

    def cmd_emi(self, args: List[str]):
        try:
            principal = float(input("Loan principal ₹: "))
            rate = float(input("Annual interest %: "))
            years = float(input("Tenure (years): "))
            m = emi(principal, rate, years)
            total = m * int(round(years * 12))
            interest = total - principal
            print(f"EMI ≈ {currency(m)} | Total Interest ≈ {currency(interest)} | Total Paid ≈ {currency(total)}")
        except Exception:
            print("Please enter valid numbers.")

    def cmd_retirement(self, args: List[str]):
        try:
            age = self.profile.age or int(input("Your age: "))
            retire_age = int(input("Retirement age (e.g., 60): "))
            years_to_retire = retire_age - age
            if years_to_retire <= 0:
                print("Retirement age must be greater than current age.")
                return
            expense = self.profile.monthly_expenses or float(input("Monthly expense today ₹: "))
            inflation = float(input("Inflation % (default 6): ") or 6.0)
            swr = float(input("Safe withdrawal rate % (default 3.5): ") or 3.5)
            corpus, future_monthly = retirement_target(expense, years_to_retire, retired_years=25, inflation_pct=inflation, swr_pct=swr)
            print(f"At {inflation:.1f}% inflation, your monthly expense at retirement ≈ {currency(future_monthly)}")
            print(f"Estimated retirement corpus needed (SWR {swr:.2f}%) ≈ {currency(corpus)}")
            rate = float(input("Expected return during accumulation % (e.g., 12): ") or 12)
            sip_amt = required_sip(corpus, rate, years_to_retire)
            print(f"Suggested monthly investment ≈ {currency(sip_amt)} for {years_to_retire} years at {rate:.1f}% p.a.")
        except Exception as e:
            print(f"Please enter valid numbers. {e}")

    def cmd_allocate(self, args: List[str]):
        try:
            risk = input(f"Risk (conservative/moderate/aggressive) [{self.profile.risk}]: ").strip() or self.profile.risk
            horizon = int(input("Investment horizon (years): "))
            allocation = suggest_allocation(risk, horizon)
            print("Suggested allocation:")
            for k, v in allocation.items():
                print(f"  {k.capitalize():8s}: {int(v*100)}%")
        except Exception:
            print("Please enter valid inputs.")

    def cmd_tax(self, args: List[str]):
        try:
            income = float(input("Gross annual income ₹: "))
            salaried = (input("Are you salaried? (y/n) [y]: ").strip().lower() or 'y') == 'y'
            regime_default = self.profile.regime_preference or 'new'
            regime = input(f"Regime new/old [{regime_default}]: ").strip().lower() or regime_default
            d80c = 0.0
            d80d = 0.0
            if regime == 'old':
                d80c = float(input("80C deductions ₹ (max 1.5L): ") or 0)
                d80d = float(input("80D health insurance ₹ (max 25k): ") or 0)
            res = self.tax_calc.estimate(income, salaried, regime, d80c, d80d)
            print("\n— Tax Estimate —")
            print(f"Regime: {'New' if res['regime']==1.0 else 'Old'}")
            print(f"Gross Income:      {currency(res['gross'])}")
            if res['std_deduction']:
                print(f"Std Deduction:     {currency(res['std_deduction'])}")
            if res['deductions']:
                print(f"Other Deductions:  {currency(res['deductions'])}")
            print(f"Taxable Income:    {currency(res['taxable'])}")
            print(f"Base Tax:          {currency(res['base_tax'])}")
            if res['rebate']:
                print(f"Rebate (87A):      -{currency(res['rebate'])}")
            print(f"Cess (4%):         {currency(res['cess'])}")
            print(f"Total Tax:         {currency(res['total_tax'])}")
            print(f"Effective Rate:    {res['effective_rate']*100:.2f}%\n")
        except Exception as e:
            print(f"Please enter valid numbers. {e}")

    # ------------------- Intent Parsing (lightweight) -------------------
    def handle_freeform(self, text: str) -> bool:
        t = text.lower()
        # Quick routes
        if any(k in t for k in ["help", "menu", "what can you do"]):
            self.cmd_help([]); return True
        if "tax" in t:
            self.cmd_tax([]); return True
        if "emi" in t:
            self.cmd_emi([]); return True
        if "retire" in t:
            self.cmd_retirement([]); return True
        if "emergency" in t:
            self.cmd_emergency([]); return True
        if "allocate" in t or "allocation" in t:
            self.cmd_allocate([]); return True
        if "sip" in t or "s.i.p" in t:
            self.cmd_sip([]); return True
        if "goal" in t:
            self.cmd_goal([]); return True
        if "profile" in t:
            self.cmd_profile([]); return True
        return False

    # ------------------- Main Loop -------------------
    def run(self):
        print("""
👋 Personal Finance Chatbot (India)
Type 'help' to see options. Type 'quit' to exit and save.
Disclaimer: Educational guidance only. Not investment/tax advice.
""".strip())
        while True:
            try:
                raw = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break
            if not raw:
                continue
            if raw.lower() in ("quit", "exit", "q"):  
                print("Saving profile… bye!")
                save_profile(self.profile)
                break
            # Try freeform intents first
            if self.handle_freeform(raw):
                continue
            # Or parse as structured command
            parts = raw.split()
            cmd, args = parts[0].lower(), parts[1:]
            handler = {
                "help": self.cmd_help,
                "profile": self.cmd_profile,
                "emergency": self.cmd_emergency,
                "sip": self.cmd_sip,
                "goal": self.cmd_goal,
                "emi": self.cmd_emi,
                "retirement": self.cmd_retirement,
                "allocate": self.cmd_allocate,
                "tax": self.cmd_tax,
            }.get(cmd)
            if handler:
                handler(args)
            else:
                print("I didn't catch that. Type 'help' for options.")


if __name__ == "__main__":
    Chatbot().run()