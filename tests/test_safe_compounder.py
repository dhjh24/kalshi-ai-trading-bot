from src.strategies.safe_compounder import SafeCompounder


def test_calculate_position_size_uses_total_account_value():
    compounder = SafeCompounder(client=object())
    opportunity = {
        "our_price": 0.95,
        "true_no_prob": 0.99,
    }

    contracts = compounder._calculate_position_size(
        opportunity,
        account_value=5563,
        cash=5563,
    )

    assert contracts == 5


def test_calculate_position_size_respects_cash_and_min_contract_size():
    compounder = SafeCompounder(client=object())
    opportunity = {
        "our_price": 0.95,
        "true_no_prob": 0.99,
    }

    assert (
        compounder._calculate_position_size(
            opportunity,
            account_value=10000,
            cash=150,
        )
        == 1
    )
    assert (
        compounder._calculate_position_size(
            opportunity,
            account_value=50,
            cash=50,
        )
        == 0
    )
