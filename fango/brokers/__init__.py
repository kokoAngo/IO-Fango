"""Brokers (中介): broker agents, their inventory scoping, and inquiry routing.

A broker is an ordinary ``agents`` row with ``vendor='broker'`` plus a 1:1
``brokers`` profile row. Brokers are external MCP agents that manage their own
listings and answer customer consults that get auto-routed to them. See
:mod:`fango.brokers.service` (identity + profile + inventory) and
:mod:`fango.brokers.inquiries` (consult → broker routing + poll/ack journal).
"""
