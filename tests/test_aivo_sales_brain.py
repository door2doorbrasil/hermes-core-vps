from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business.aivo.prompts import build_intent_prompt
from business.aivo.sales_brain import SalesBrain
from business.aivo.intents import Intent


def test_sales_brain_recognizes_payment_terms_as_purchase_or_price():
    brain = SalesBrain()

    purchase = brain.evaluate("quero 2x sem juros no cartao")
    assert purchase["intent"] in {Intent.BUY, Intent.PRICE}
    assert purchase["lead_state"].name == "BUYING"

    price = brain.evaluate("qual a condicao em ate 12x com acrescimos?")
    assert price["intent"] == Intent.PRICE
    assert price["lead_state"].name == "BUYING"


def test_price_prompt_mentions_payment_terms():
    prompt = build_intent_prompt(Intent.PRICE)
    assert "2x sem juros no cartao de credito" in prompt
    assert "ate 12x com acrescimos no cartao de credito" in prompt

