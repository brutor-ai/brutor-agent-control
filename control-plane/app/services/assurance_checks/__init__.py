# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Continuous Assurance Checks — control-plane side.

* ``core_client`` — compile/backtest against the Rust core's evaluator
  (the single CEL engine; the CP never evaluates expressions itself).
* ``judge`` — the tier-B semantic evaluator: samples finalized runs and
  judges them THROUGH the gateway proxy, budget-capped.
* ``scheduler`` — the batch worker: windowed (rate_over) tier-A
  thresholds + tier-B judge batches.
"""
