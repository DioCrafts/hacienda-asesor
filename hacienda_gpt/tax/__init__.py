"""Deterministic tax computation: parameters are data, arithmetic is code.

The language model never computes a tax figure. It gathers the user's numbers,
calls :func:`hacienda_gpt.tax.irpf.calcular_irpf`, and explains the result.
"""
