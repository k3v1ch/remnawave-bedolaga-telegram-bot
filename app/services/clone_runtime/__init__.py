"""Multi-tenant cloner runtime for white-label clone bots.

One shared ``cloner`` process hosts ALL clone bots: a single shop Dispatcher
(``build_shop_dispatcher``) + a per-tenant ``Bot`` registry, fed via a generic webhook
route. Bots are added/removed/reloaded live (hot-swap, no restart) via Redis pub/sub.
"""
