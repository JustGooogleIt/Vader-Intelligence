"""Execution ledger, risk layer, fractional Kelly sizing and the paper simulator.

Nothing in this package reaches a network: the live exchange adapter implements
`gateway.ExchangeGateway` separately. Paper and live state live in different ledger
files by construction (see `ledger`).
"""
