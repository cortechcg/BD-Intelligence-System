"""Cortech BD dashboard — a control surface over the existing pipeline.

This package adds an HTTP entry point. It does not reimplement any pipeline
logic: triggering calls straight into ``main.submit_single_url``, which is the
same function ``python main.py --submit-url`` uses.

Nothing here can submit to a client. See ``docs/DASHBOARD.md``.
"""
