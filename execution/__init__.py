"""Execution layer: the only place order intents become real Roostoo API calls.

`RoostooClient` signs and rate-limits raw REST calls. `RoostooBroker` translates
core models to/from those calls. `Portfolio` keeps a per-poll view of
positions/equity. `OrderManager` owns order submission and cancellation.
"""
