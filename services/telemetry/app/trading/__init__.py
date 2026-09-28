"""Deterministic eToro crypto pipeline: SCOUT -> QUANT -> GUARD -> TRADER, run by MANAGER.

Runs as its own process (``python -m app.trading run``). Order routes live only
in ``broker.py``; the dashboard reads the trader's database and cannot trade.
"""
