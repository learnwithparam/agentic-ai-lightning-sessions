"""What one model call costs: the crew model's list price per million tokens, input and output.

The board, the trace and the budget all price tokens here, so a card, its trace and the alert agree.
"""

PRICE_IN, PRICE_OUT = 0.10, 0.50


def cost(tokens_in: int, tokens_out: int) -> float:
    return (tokens_in * PRICE_IN + tokens_out * PRICE_OUT) / 1e6
